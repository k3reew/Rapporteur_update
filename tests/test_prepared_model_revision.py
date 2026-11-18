import io
import json
import sqlite3
from pathlib import Path

import pytest

from rapporteur import preparation, settings
from rapporteur.errors import StageError
from rapporteur.llm.client import ScriptedClient
from rapporteur.preparation import prepared_key
from rapporteur.storage import Storage

from .fictional_talk import PROFILE


def test_prepared_recording_key_tracks_active_whisper_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    recording = conn.execute("SELECT 'abc' AS sha256, 'audio' AS kind").fetchone()
    document = conn.execute("SELECT 'abc' AS sha256, 'document' AS kind").fetchone()
    keys = []
    document_keys = []
    for revision in ("first", "second"):
        directory = tmp_path / "revisions" / revision
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({"revision": revision}))
        (tmp_path / "active.json").write_text(json.dumps(f"revisions/{revision}"))
        keys.append(prepared_key(recording, "profile"))
        document_keys.append(prepared_key(document, "profile"))
    conn.close()
    assert keys[0] != keys[1]
    assert document_keys[0] == document_keys[1]


@pytest.mark.parametrize("switch_during_request", [False, True])
def test_mismatched_asr_revision_never_enters_prepared_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    switch_during_request: bool,
) -> None:
    root = tmp_path / "models"
    for revision in ("old", "new"):
        directory = root / "revisions" / revision
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({"revision": revision}))
    selector = root / "active.json"
    selector.write_text(json.dumps("revisions/new"))
    monkeypatch.setenv("ASR_MODEL_DIR", str(root))
    monkeypatch.setattr(settings, "ASR_ENABLED", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    recording = conn.execute("SELECT ? AS sha256, 'audio' AS kind", ("a" * 64,)).fetchone()
    storage = Storage(tmp_path / "storage")
    key = prepared_key(recording, PROFILE.model_dump_json())

    def answer(*args: object, **kwargs: object) -> io.BytesIO:
        if switch_during_request:
            selector.write_text(json.dumps("revisions/old"))
        return io.BytesIO(
            json.dumps(
                {
                    "revision": json.dumps({"revision": "old"}),
                    "segments": [
                        {"start_ms": 0, "end_ms": 1000, "text": "Старая модель"},
                    ],
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", answer)
    with pytest.raises(StageError, match="ревизи"):
        preparation.extract(recording, storage, ScriptedClient([]), PROFILE)
    conn.close()
    assert not storage.exists(key)
