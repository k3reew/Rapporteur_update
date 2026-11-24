"""HTTP endpoints for system."""

import os

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from rapporteur import (
    model_choice,
    settings,
)
from rapporteur.api.dependencies import _database
from rapporteur.contracts import ModelProfile
from rapporteur.contracts.enums import InferenceEngine
from rapporteur.errors import UserError
from rapporteur.llm.profiles import ProfileError, ProfilesFile, load_profile, local_models

api = APIRouter()


class ProfileInfo(BaseModel):
    name: str
    model: str
    engine: InferenceEngine
    reasoning: bool
    prompt_version: str
    # Задан RAPPORTEUR_PROFILE или полем default в profiles.yaml.
    default: bool
    # Модель есть в каталоге OLLAMA_MODELS_DIR; None — каталог не подключён.
    available: bool | None = None
    # Профиль, по которому приложение работает: заданный или замена с имеющейся моделью.
    used: bool = False


class TemplateInfo(BaseModel):
    name: str
    version: str


class InferenceReplica(BaseModel):
    name: str
    ready: bool


class SystemInfo(BaseModel):
    version: str
    ollama_url: str
    ollama_available: bool
    ollama_models: list[str]
    inference_engine: InferenceEngine
    inference_available: bool
    inference_models: list[str]
    inference_replicas: list[InferenceReplica]
    profiles: list[ProfileInfo]
    # Почему используется не заданный профиль: модели нет в каталоге моделей.
    profile_note: str | None = None
    templates: list[TemplateInfo]
    # Доклады по состоянию обработки: queued, running, failed.
    queue: dict[str, int]


def _profiles() -> tuple[list[ProfileInfo], str | None, ModelProfile | None]:
    try:
        data = ProfilesFile.model_validate(
            yaml.safe_load(settings.PROFILES_FILE.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError):
        return [], None, None
    configured = os.environ.get("RAPPORTEUR_PROFILE") or data.default
    present = local_models()
    try:
        used: ModelProfile | None = load_profile()
        note = None
    except ProfileError as error:
        used, note = None, str(error)
    wanted = next((item for item in data.profiles if item.name == configured), None)
    if used is not None and wanted is not None and used.model != wanted.model:
        note = (
            f"Модели {wanted.model} профиля {configured} нет в каталоге моделей "
            f"(OLLAMA_MODELS_DIR), используется {used.name} ({used.model})"
        )
    profiles = [
        _profile(
            item,
            item.name == configured,
            item.model in present if present and item.engine is InferenceEngine.OLLAMA else None,
            used is not None and item.name == used.name,
        )
        for item in data.profiles
    ]
    return profiles, note, used


def _profile(item: ModelProfile, default: bool, available: bool | None, used: bool) -> ProfileInfo:
    return ProfileInfo(
        name=item.name,
        model=item.model,
        engine=item.engine,
        reasoning=item.generation.reasoning,
        prompt_version=item.prompt_version,
        default=default,
        available=available,
        used=used,
    )


def _templates() -> list[TemplateInfo]:
    result = []
    for spec in sorted(settings.TEMPLATES_DIR.glob("*/variables.yaml")):
        try:
            template_version = yaml.safe_load(spec.read_text(encoding="utf-8")).get("version")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if isinstance(template_version, str):
            result.append(TemplateInfo(name=spec.parent.name, version=template_version))
    return result


@api.get("/system")
def system(request: Request) -> SystemInfo:
    # Запрос к модели не держит транзакцию.
    with _database(request).read() as conn:
        queue = {
            row["job"]: row["count"]
            for row in conn.execute(
                "SELECT job, count(*) AS count FROM talks "
                "WHERE job IN ('queued', 'running', 'failed', 'paused') GROUP BY job"
            )
        }
    profiles, note, used = _profiles()
    engine = used.engine if used else InferenceEngine.OLLAMA
    ollama_models: list[model_choice.OllamaModel] | None = None
    replicas = []
    if used is not None and engine is InferenceEngine.VLLM:
        served = [model_choice.vllm_models(url) for url in settings.vllm_endpoints()]
        replicas = [
            InferenceReplica(name=f"Экземпляр {index}", ready=used.model in names)
            for index, names in enumerate(served, 1)
        ]
        models = sorted({model for names in served for model in names})
        available = used.model in models
        for item in profiles:
            if item.engine is engine:
                item.available = item.model in models
    else:
        ollama_models = model_choice.installed_models(settings.OLLAMA_URL)
        models = [item.name for item in ollama_models or []]
        available = ollama_models is not None
    return SystemInfo(
        version=request.app.version,
        ollama_url=settings.OLLAMA_URL,
        ollama_available=ollama_models is not None,
        ollama_models=[item.name for item in ollama_models or []],
        inference_engine=engine,
        inference_available=available,
        inference_models=models,
        inference_replicas=replicas,
        profiles=profiles,
        profile_note=note,
        templates=_templates(),
        queue=queue,
    )


@api.get("/model")
def get_model(request: Request) -> model_choice.ModelState:
    """Текущая модель и доступность сервиса."""
    fixed = load_profile().engine is InferenceEngine.VLLM
    models = None if fixed else model_choice.installed_models(settings.OLLAMA_URL)
    served = (
        [name for url in settings.vllm_endpoints() for name in model_choice.vllm_models(url)]
        if fixed
        else None
    )
    with _database(request).read() as conn:
        return model_choice.state(conn, models, served=served)


@api.put("/model")
def set_model(request: Request, data: model_choice.ModelChoiceWrite) -> model_choice.ModelState:
    """Новая модель применяется к докладам, которые обрабатываются после сохранения."""
    if load_profile().engine is InferenceEngine.VLLM:
        raise UserError("Модель vLLM задаётся в конфигурации сервера", 409)
    models = model_choice.installed_models(settings.OLLAMA_URL)
    with _database(request).tx() as conn:
        model_choice.save(conn, data, models)
        return model_choice.state(conn, models)
