"""Internal GPU service. Only content-addressed local blobs may be transcribed."""

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StringConstraints

from rapporteur.storage import Storage, blob_key, cache_key

app = FastAPI(docs_url=None, redoc_url=None)
_lock = Lock()
_model: Any = None
CHUNK_SECONDS = 300


class Input(BaseModel):
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def model() -> Any:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        device = os.environ.get("ASR_DEVICE", "cuda")
        compute_type = os.environ.get("ASR_COMPUTE_TYPE") or (
            "int8" if device == "cpu" else "float16"
        )
        _model = WhisperModel(
            os.environ.get("ASR_MODEL_DIR", "/models/asr"),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
    return _model


@app.get("/health")
def health() -> dict[str, str]:
    if not (Path(os.environ.get("ASR_MODEL_DIR", "/models/asr")) / "model.bin").is_file():
        raise HTTPException(503, "ASR model.bin отсутствует")
    return {"status": "ok"}


@app.post("/transcribe")
def transcribe(data: Input) -> dict[str, Any]:
    storage = Storage(Path("/data"))
    source = storage.path(blob_key(data.sha256))
    if not source.is_file():
        raise HTTPException(404, "Оригинал не найден")
    # A read-only models directory is fingerprinted by the offline preparation manifest.
    manifest = Path(os.environ.get("ASR_MODEL_DIR", "/models/asr")) / "manifest.json"
    revision = (
        manifest.read_text() if manifest.is_file() else os.environ.get("ASR_REVISION", "small")
    )
    with _lock, tempfile.TemporaryDirectory(prefix="asr-") as tmp:
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(source),
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            if not math.isfinite(duration) or not 0 < duration <= 36000:
                raise ValueError("Длительность должна быть не больше 10 часов")
            result = []
            for start in range(0, math.ceil(duration), CHUNK_SECONDS):
                key = (
                    f"cache/asr/{cache_key(data.sha256, revision, start, CHUNK_SECONDS, 'v1')}.json"
                )
                if storage.exists(key):
                    result.extend(json.loads(storage.get(key)))
                    continue
                audio = Path(tmp) / "chunk.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        "file",
                        "-ss",
                        str(start),
                        "-i",
                        str(source),
                        "-t",
                        str(CHUNK_SECONDS),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(audio),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                segments, _ = model().transcribe(
                    str(audio), vad_filter=True, beam_size=5, condition_on_previous_text=False
                )
                chunk = [
                    {
                        "start_ms": round((start + s.start) * 1000),
                        "end_ms": min(round(duration * 1000), round((start + s.end) * 1000)),
                        "text": s.text.strip(),
                    }
                    for s in segments
                    if s.text.strip() and s.end > s.start
                ]
                storage.put(key, json.dumps(chunk, ensure_ascii=False).encode())
                result.extend(chunk)
            return {"segments": result, "revision": revision}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise HTTPException(
                422, "Ошибка локального распознавания: " + str(error)[:300]
            ) from error
