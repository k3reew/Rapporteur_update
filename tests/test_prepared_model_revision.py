import sqlite3
from pathlib import Path

from rapporteur import preparation
from rapporteur.contracts import FragmentDraft
from rapporteur.llm.client import ScriptedClient
from rapporteur.storage import Storage

from .fictional_talk import PROFILE


def test_archived_recording_is_read_from_an_old_cache_without_whisper(tmp_path: Path) -> None:
    sha = "a" * 64
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        recording = conn.execute("SELECT ? AS sha256, 'audio' AS kind", (sha,)).fetchone()
    storage = Storage(tmp_path)
    saved = preparation.Prepared(
        fragments=[
            FragmentDraft.model_validate(
                {
                    "kind": "transcript",
                    "source_sha256": sha,
                    "locator": {"kind": "time", "start_ms": 0, "end_ms": 1000},
                    "raw_text": "Архивная запись",
                    "normalized_text": "Архивная запись",
                    "origin": "asr",
                    "engine_confidence": None,
                }
            ),
        ],
    )
    key = preparation.prepared_key(recording, "old-profile", asr_revision="old-model-manifest")
    storage.put(key, saved.model_dump_json().encode())
    assert preparation.extract(recording, storage, ScriptedClient([]), PROFILE) == saved
    assert storage.get(key) == saved.model_dump_json().encode()
