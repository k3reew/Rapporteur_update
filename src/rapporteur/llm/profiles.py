"""Профили моделей из profiles.yaml (spec §9, ADR-010)."""

import hashlib
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from rapporteur.contracts import ModelProfile
from rapporteur.settings import PROFILES_FILE


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


def load_profile(name: str | None = None, path: Path = PROFILES_FILE) -> ModelProfile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        profiles = ProfilesFile.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ProfileError(f"Профили моделей не читаются из {path}: {error}") from error
    wanted = name or os.environ.get("RAPPORTEUR_PROFILE") or profiles.default
    for profile in profiles.profiles:
        if profile.name == wanted:
            if profile.weights_revision == "local-manifest":
                root = Path(os.environ.get("OLLAMA_MODELS_PATH", "models/ollama"))
                model, tag = profile.model.split(":", 1)
                manifest = root / "manifests" / "registry.ollama.ai" / "library" / model / tag
                if not manifest.is_file():
                    manifest = (
                        root / "manifests-v2" / "registry.ollama.ai" / "library" / model / tag
                    )
                try:
                    revision = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
                except OSError as error:
                    raise ProfileError(f"Нет манифеста серверной модели: {manifest}") from error
                return profile.model_copy(update={"weights_revision": revision})
            return profile
    known = ", ".join(profile.name for profile in profiles.profiles)
    raise ProfileError(f"Нет профиля {wanted}; есть: {known}")
