"""Профиль работает только с моделями, которые лежат в каталоге OLLAMA_MODELS_DIR."""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur.llm.profiles import ProfileError, load_profile, local_models


def add_model(root: Path, name: str, size: int = 10, quantization: str = "Q4_K_M") -> Path:
    """Манифест и конфиг модели, как их раскладывает Ollama."""
    model, tag = name.split(":")
    repository = model.split("/") if "/" in model else ["library", model]
    manifest = root / "manifests" / "registry.ollama.ai" / Path(*repository) / tag
    manifest.parent.mkdir(parents=True, exist_ok=True)
    digest = f"sha256:{abs(hash(name)):064x}"[:71]
    manifest.write_text(
        json.dumps(
            {
                "config": {"digest": digest},
                "layers": [{"mediaType": "application/vnd.ollama.image.model", "size": size}],
            }
        )
    )
    (root / "blobs").mkdir(exist_ok=True)
    (root / "blobs" / digest.replace(":", "-")).write_text(json.dumps({"file_type": quantization}))
    return manifest


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OLLAMA_MODELS_PATH", str(tmp_path))
    return tmp_path


def test_catalog_lists_models_by_ollama_names(catalog: Path) -> None:
    add_model(catalog, "qwen3.5:9b")
    add_model(catalog, "someone/tuned:v1")
    other = catalog / "manifests-v2" / "hf.co" / "org" / "model" / "q4"
    other.parent.mkdir(parents=True)
    other.write_text("{}")
    assert sorted(local_models()) == ["hf.co/org/model:q4", "qwen3.5:9b", "someone/tuned:v1"]


def test_missing_server_model_falls_back_to_a_profile_with_present_model(
    catalog: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    add_model(catalog, "qwen3.5:9b")
    monkeypatch.setenv("RAPPORTEUR_PROFILE", "server")
    with caplog.at_level(logging.WARNING, logger="rapporteur.profiles"):
        first = load_profile()
        assert load_profile() == first
    assert (first.name, first.model) == ("local", "qwen3.5:9b")
    # Предупреждение одно, хотя профиль читается многократно.
    assert caplog.text.count("Модели qwen3.5:27b профиля server нет") == 1
    # Явно названный профиль не подменяется: его модели нет — это ошибка.
    with pytest.raises(ProfileError, match=r"qwen3\.5:27b профиля server нет в каталоге"):
        load_profile("server")


def test_present_server_model_is_used_with_its_manifest_revision(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_model(catalog, "qwen3.5:9b")
    add_model(catalog, "qwen3.5:27b")
    monkeypatch.setenv("RAPPORTEUR_PROFILE", "server")
    profile = load_profile()
    assert (profile.name, profile.model) == ("server", "qwen3.5:27b")
    assert profile.weights_revision.startswith("sha256:")


def test_without_matching_profile_the_largest_present_model_is_used(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_model(catalog, "gemma4:e4b", size=8, quantization="Q8_0")
    add_model(catalog, "qwen2.5:7b", size=5)
    monkeypatch.setenv("RAPPORTEUR_PROFILE", "server")
    profile = load_profile()
    assert (profile.name, profile.model, profile.quantization) == (
        "gemma4:e4b",
        "gemma4:e4b",
        "Q8_0",
    )
    assert profile.generation.reasoning is False
    assert profile.prompt_version == load_profile("local").prompt_version


def test_settings_page_reports_missing_profile_models(
    catalog: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur import db, model_choice
    from rapporteur.app import create_app

    add_model(catalog, "qwen3.5:9b")
    monkeypatch.setenv("RAPPORTEUR_PROFILE", "server")
    monkeypatch.setattr(model_choice, "installed_models", lambda *args, **kwargs: None)
    application = create_app(tmp_path / "data", process=False)
    with TestClient(application) as client:
        info = client.get("/api/v1/system").json()
        state = client.get("/api/v1/model")
    rows = {item["name"]: item for item in info["profiles"]}
    assert (rows["server"]["default"], rows["server"]["available"], rows["server"]["used"]) == (
        True,
        False,
        False,
    )
    assert (rows["local"]["available"], rows["local"]["used"]) == (True, True)
    assert "qwen3.5:27b профиля server нет" in info["profile_note"]
    assert state.status_code == 200, state.text
    assert state.json()["default"]["model"] == "qwen3.5:9b"

    # Выбор на странице «Управление» модели, которой больше нет в каталоге, не используется.
    with application.state.database.tx() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('model', ?)",
            (
                json.dumps(
                    {
                        "model": "qwen3.5:4b",
                        "reasoning": False,
                        "weights_revision": "sha256:x",
                        "quantization": "Q4_K_M",
                    }
                ),
            ),
        )
    with application.state.database.read() as conn:
        assert model_choice.active_profile(conn).model == "qwen3.5:9b"
    add_model(catalog, "qwen3.5:4b")
    with db.Database(tmp_path / "data" / "rapporteur.db").read() as conn:
        assert model_choice.active_profile(conn).model == "qwen3.5:4b"
