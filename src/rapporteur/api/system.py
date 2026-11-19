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
from rapporteur.llm.profiles import ProfilesFile

api = APIRouter()


class ProfileInfo(BaseModel):
    name: str
    model: str
    reasoning: bool
    prompt_version: str
    default: bool


class TemplateInfo(BaseModel):
    name: str
    version: str


class SystemInfo(BaseModel):
    version: str
    ollama_url: str
    ollama_available: bool
    ollama_models: list[str]
    # Распознавание речи запущено (ASR_MODE); без него записи не расшифровываются.
    asr_enabled: bool
    profiles: list[ProfileInfo]
    templates: list[TemplateInfo]
    # Доклады по состоянию обработки: queued, running, failed.
    queue: dict[str, int]


def _profiles() -> list[ProfileInfo]:
    try:
        data = ProfilesFile.model_validate(
            yaml.safe_load(settings.PROFILES_FILE.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError):
        return []
    return [
        _profile(item, item.name == (os.environ.get("RAPPORTEUR_PROFILE") or data.default))
        for item in data.profiles
    ]


def _profile(item: ModelProfile, default: bool) -> ProfileInfo:
    return ProfileInfo(
        name=item.name,
        model=item.model,
        reasoning=item.generation.reasoning,
        prompt_version=item.prompt_version,
        default=default,
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
    # Запрос к Ollama идёт вне транзакции: он не должен задерживать обработку.
    with _database(request).read() as conn:
        queue = {
            row["job"]: row["count"]
            for row in conn.execute(
                "SELECT job, count(*) AS count FROM talks "
                "WHERE job IN ('queued', 'running', 'failed', 'paused') GROUP BY job"
            )
        }
    models = model_choice.installed_models(settings.OLLAMA_URL)
    return SystemInfo(
        version=request.app.version,
        ollama_url=settings.OLLAMA_URL,
        ollama_available=models is not None,
        ollama_models=[item.name for item in models or []],
        asr_enabled=settings.ASR_ENABLED,
        profiles=_profiles(),
        templates=_templates(),
        queue=queue,
    )


@api.get("/model")
def get_model(request: Request) -> model_choice.ModelState:
    """Модель для сводок: текущая, по умолчанию и установленные в Ollama."""
    models = model_choice.installed_models(settings.OLLAMA_URL)
    with _database(request).read() as conn:
        return model_choice.state(conn, models)


@api.put("/model")
def set_model(request: Request, data: model_choice.ModelChoiceWrite) -> model_choice.ModelState:
    """Новая модель применяется к докладам, которые обрабатываются после сохранения."""
    models = model_choice.installed_models(settings.OLLAMA_URL)
    with _database(request).tx() as conn:
        model_choice.save(conn, data, models)
        return model_choice.state(conn, models)
