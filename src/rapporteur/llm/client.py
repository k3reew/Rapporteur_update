"""Клиент-абстракция LLM (ADR-010). Шаги сводки видят запрос и ответ; движок — забота клиента."""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from queue import SimpleQueue
from typing import Any, Literal, Protocol

from rapporteur.contracts import ModelProfile
from rapporteur.contracts.enums import InferenceEngine
from rapporteur.errors import FailureCode, StageError

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    # JSON Schema ответа; грамматика проверяет только форму, связи полей — Pydantic.
    schema: dict[str, Any] | None
    think: bool
    temperature: float
    max_tokens: int
    context_tokens: int
    seed: int | None = None


@dataclass(frozen=True)
class ChatResponse:
    content: str
    thinking: str
    # stop — ответ закончен, length — оборван лимитом токенов.
    done_reason: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float


class LlmClient(Protocol):
    def chat(self, request: ChatRequest) -> ChatResponse: ...


class OllamaClient:
    """/api/chat Ollama: think, format, num_ctx, num_predict (ADR-010)."""

    def __init__(self, base_url: str, timeout_s: float = 1800) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def chat(self, request: ChatRequest) -> ChatResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": m.role, "content": m.content, **({"images": m.images} if m.images else {})}
                for m in request.messages
            ],
            "stream": False,
            "think": request.think,
            "options": {
                "temperature": request.temperature,
                "num_ctx": request.context_tokens,
                "num_predict": request.max_tokens,
                **({"seed": request.seed} if request.seed is not None else {}),
            },
        }
        if request.schema is not None:
            body["format"] = request.schema
        http_request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            json.dumps(body, ensure_ascii=False).encode(),
            {"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise StageError(
                FailureCode.LLM_UNAVAILABLE, f"Ollama ответила {error.code}: {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise StageError(
                FailureCode.LLM_UNAVAILABLE, f"Ollama недоступна по {self.base_url}: {error}"
            ) from error
        message = data.get("message") or {}
        return ChatResponse(
            content=message.get("content") or "",
            thinking=message.get("thinking") or "",
            done_reason=data.get("done_reason") or "",
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            seconds=time.monotonic() - started,
        )


class VllmClient:
    """Локальный vLLM с OpenAI-совместимым протоколом и шаблоном Qwen."""

    def __init__(self, base_url: str, timeout_s: float = 1800) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        if not message.images:
            return {"role": message.role, "content": message.content}
        return {
            "role": message.role,
            "content": [
                {"type": "text", "text": message.content},
                *[
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}
                    for image in message.images
                ],
            ],
        }

    def chat(self, request: ChatRequest) -> ChatResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message(message) for message in request.messages],
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": request.think},
            "temperature": request.temperature,
            "max_completion_tokens": request.max_tokens,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": request.schema},
            }
        http_request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            json.dumps(body, ensure_ascii=False).encode(),
            {"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                detail = (
                    error.read(4096).decode(errors="replace").lower() if error.code == 400 else ""
                )
            if "maximum context length" in detail or "max_model_len" in detail:
                raise StageError(
                    FailureCode.CONTEXT_OVERFLOW, "Запрос превышает предел контекста vLLM"
                ) from error
            raise StageError(
                FailureCode.LLM_UNAVAILABLE,
                f"vLLM не выполнил запрос (HTTP {error.code}). Проверьте состояние модели.",
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise StageError(
                FailureCode.LLM_UNAVAILABLE, "vLLM недоступен. Проверьте состояние модели."
            ) from error
        except ValueError as error:
            raise StageError(
                FailureCode.LLM_INVALID_OUTPUT, "Ответ vLLM не является JSON"
            ) from error
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            thinking = message.get("reasoning") or ""
            reason = choice["finish_reason"]
            if not isinstance(content, str) or not isinstance(thinking, str):
                raise ValueError("invalid message")
            if reason not in ("stop", "length"):
                raise ValueError("invalid finish reason")
            if (
                reason == "stop"
                and request.schema is not None
                and request.think
                and not content.strip()
            ):
                # vLLM может вернуть весь структурированный ответ в reasoning (vllm#50948).
                try:
                    recovered = json.loads(thinking)
                except json.JSONDecodeError:
                    recovered = None
                if isinstance(recovered, dict):
                    content, thinking = thinking, ""
            usage = data["usage"]
            return ChatResponse(
                content=content,
                thinking=thinking,
                done_reason=reason,
                prompt_tokens=int(usage["prompt_tokens"]),
                completion_tokens=int(usage["completion_tokens"]),
                seconds=time.monotonic() - started,
            )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise StageError(FailureCode.LLM_INVALID_OUTPUT, "Некорректный ответ vLLM") from error


class VllmPool:
    """Один одновременно выполняемый запрос на отдельный экземпляр модели."""

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.free: SimpleQueue[VllmClient] = SimpleQueue()
        for endpoint in endpoints:
            self.free.put(VllmClient(endpoint))

    def chat(self, request: ChatRequest) -> ChatResponse:
        client = self.free.get()
        try:
            return client.chat(request)
        finally:
            self.free.put(client)


class ScriptedClient:
    """Отвечает заданными ответами по очереди и запоминает запросы. Для тестов."""

    def __init__(self, responses: Iterable[ChatResponse | str]) -> None:
        self.responses = list(responses)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("у ScriptedClient закончились ответы")
        response = self.responses.pop(0)
        if isinstance(response, str):
            return ChatResponse(response, "", "stop", 100, len(response) // 3, 0.0)
        return response


def make_client(profile: ModelProfile, ollama_url: str, vllm: str | LlmClient) -> LlmClient:
    if profile.engine is InferenceEngine.OLLAMA:
        return OllamaClient(ollama_url)
    return VllmClient(vllm) if isinstance(vllm, str) else vllm
