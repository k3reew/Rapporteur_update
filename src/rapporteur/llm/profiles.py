"""Профили моделей из profiles.yaml (spec §9, ADR-010).

Профиль выбирается по RAPPORTEUR_PROFILE или полю default. vLLM использует заданную
ревизию весов. Для Ollama используется только модель,
которая действительно лежит в каталоге OLLAMA_MODELS_DIR (в контейнере — OLLAMA_MODELS_PATH).
Если модели выбранного профиля там нет, берётся первый профиль profiles.yaml с имеющейся
моделью; если подходящего профиля нет, выбранный профиль работает с самой крупной имеющейся
моделью без режима рассуждения. Замена пишется в журнал один раз. Без каталога моделей
(тесты, Ollama другого проекта без общего каталога) профиль используется как описан.
"""

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from rapporteur.contracts import ModelProfile
from rapporteur.contracts.enums import InferenceEngine
from rapporteur.settings import PROFILES_FILE

log = logging.getLogger("rapporteur.profiles")
LOCAL_MANIFEST = "local-manifest"
_MANIFESTS = ("manifests", "manifests-v2")
_OLLAMA_REGISTRY = "registry.ollama.ai"
_WEIGHTS = "application/vnd.ollama.image.model"


class ProfileError(Exception):
    pass


class ProfilesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str
    profiles: list[ModelProfile]

    @model_validator(mode="after")
    def _default_known(self) -> "ProfilesFile":
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("имена профилей повторяются")
        if self.default not in names:
            raise ValueError(f"профиль по умолчанию {self.default} не описан")
        return self


def models_root() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS_PATH", "models/ollama"))


def local_models(root: Path | None = None) -> dict[str, Path]:
    """Модели каталога Ollama по манифестам: имя, как его показывает Ollama, → манифест.

    registry.ollama.ai/library/qwen3.5/9b → qwen3.5:9b, registry.ollama.ai/user/m/t → user/m:t,
    hf.co/org/m/t → hf.co/org/m:t."""
    root = root or models_root()
    found: dict[str, Path] = {}
    for folder in _MANIFESTS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            parts = path.relative_to(base).parts
            if not path.is_file() or len(parts) < 3 or any(p.startswith(".") for p in parts):
                continue
            *repository, tag = parts
            if repository[0] == _OLLAMA_REGISTRY:
                repository = repository[1:]
                if repository[0] == "library":
                    repository = repository[1:]
            found.setdefault("/".join(repository) + ":" + tag, path)
    return found


def manifest_revision(manifest: Path) -> str:
    return "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()


def load_profile(name: str | None = None, path: Path = PROFILES_FILE) -> ModelProfile:
    """Профиль по имени — ровно он; без имени — выбранный с учётом имеющихся моделей."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        profiles = ProfilesFile.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ProfileError(f"Профили моделей не читаются из {path}: {error}") from error
    wanted_name = name or os.environ.get("RAPPORTEUR_PROFILE") or profiles.default
    wanted = next((item for item in profiles.profiles if item.name == wanted_name), None)
    if wanted is None:
        known = ", ".join(profile.name for profile in profiles.profiles)
        raise ProfileError(f"Нет профиля {wanted_name}; есть: {known}")
    if wanted.engine is InferenceEngine.VLLM:
        return wanted
    present = local_models()
    if name is not None or not present or wanted.model in present:
        return _pinned(wanted, present.get(wanted.model))
    root = models_root()
    for profile in profiles.profiles:
        if profile.engine is wanted.engine and profile.model in present:
            _replaced(wanted.name, wanted.model, f"профиль {profile.name}", profile.model, root)
            return _pinned(profile, present[profile.model])
    model, manifest = max(present.items(), key=lambda item: (_weights_size(item[1]), item[0]))
    _replaced(wanted.name, wanted.model, "модель", model, root)
    return wanted.model_copy(
        update={
            "name": model,
            "model": model,
            "weights_revision": manifest_revision(manifest),
            "quantization": _quantization(manifest, root),
            # Есть ли у модели режим рассуждения, манифест не говорит: без него безопаснее.
            "generation": wanted.generation.model_copy(update={"reasoning": False}),
        }
    )


def _pinned(profile: ModelProfile, manifest: Path | None) -> ModelProfile:
    """local-manifest заменяется хешем манифеста из каталога моделей."""
    if profile.weights_revision != LOCAL_MANIFEST:
        return profile
    if manifest is None:
        manifest = _manifest_path(profile.model)
    try:
        revision = manifest_revision(manifest)
    except OSError as error:
        raise ProfileError(
            f"Модели {profile.model} профиля {profile.name} нет в каталоге моделей "
            f"(OLLAMA_MODELS_DIR): {manifest}"
        ) from error
    return profile.model_copy(update={"weights_revision": revision})


def _manifest_path(model: str) -> Path:
    name, tag = model.split(":", 1)
    repository = name.split("/") if "/" in name else ["library", name]
    if not repository[0].count("."):
        repository = [_OLLAMA_REGISTRY, *repository]
    root = models_root()
    candidates = [root / folder / Path(*repository) / tag for folder in _MANIFESTS]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _manifest(manifest: Path) -> dict[str, Any]:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _weights_size(manifest: Path) -> int:
    """Размер весов модели по манифесту: из имеющихся моделей берётся самая крупная."""
    layers = _manifest(manifest).get("layers")
    total = 0
    for layer in layers if isinstance(layers, list) else []:
        if isinstance(layer, dict) and layer.get("mediaType") == _WEIGHTS:
            total += int(layer.get("size", 0))
    return total


def _quantization(manifest: Path, root: Path) -> str:
    config = _manifest(manifest).get("config")
    digest = config.get("digest") if isinstance(config, dict) else None
    try:
        blob = root / "blobs" / str(digest).replace(":", "-")
        value = json.loads(blob.read_text(encoding="utf-8")).get("file_type")
    except (OSError, ValueError, AttributeError):
        value = None
    return str(value) if value else "неизвестно"


@lru_cache(maxsize=32)
def _replaced(wanted: str, missing: str, kind: str, used: str, root: Path) -> None:
    """Предупреждение о замене модели пишется один раз, а не при каждом чтении профиля."""
    log.warning(
        "Модели %s профиля %s нет в каталоге моделей %s (OLLAMA_MODELS_DIR); используется %s %s",
        missing,
        wanted,
        root,
        kind,
        used,
    )
