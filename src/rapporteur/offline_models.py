"""Download and verify public model artifacts without installing a model runtime."""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rapporteur.asr_models import ASR_REQUIRED_FILES, ASR_SELECTOR, check_asr_model


def read_json(url: str) -> tuple[dict[str, Any], bytes]:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    return json.loads(data), data


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 2**20):
            value.update(chunk)
    return value.hexdigest()


def multipart(
    url: str, path: Path, sha256: str | None, size: int, *, chunk_bytes: int = 256 * 2**20
) -> None:
    partial = path.with_name(path.name + ".partial")
    prefix = partial.stat().st_size if partial.exists() else 0
    if shutil.disk_usage(path.parent).free < (size - prefix) * 2 + 5 * 2**30:
        raise OSError("Не хватает места для временных частей модели и 5 ГБ запаса")
    step = chunk_bytes
    spans = [(start, min(size, start + step)) for start in range(prefix, size, step)]

    def part(span: tuple[int, int]) -> Path:
        start, end = span
        target = path.with_name(path.name + f".chunk-{start}")
        if target.is_file() and target.stat().st_size == end - start:
            return target
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}"})
                with urllib.request.urlopen(request, timeout=120) as source:
                    if (
                        source.status != 206
                        or source.headers.get("Content-Range", "").split("/")[0]
                        != f"bytes {start}-{end - 1}"
                    ):
                        raise OSError("Сервер не поддерживает проверенную загрузку по диапазонам")
                    with target.open("wb") as out:
                        remaining = end - start
                        while remaining:
                            chunk = source.read(min(4 * 2**20, remaining))
                            if not chunk:
                                raise OSError("Оборван диапазон")
                            out.write(chunk)
                            remaining -= len(chunk)
                print(
                    f"{path.name}: диапазон {start // 2**20}–{end // 2**20} MiB готов", flush=True
                )
                return target
            except OSError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    with ThreadPoolExecutor(max_workers=6) as pool:
        chunks = list(pool.map(part, spans))
    assembled = path.with_name(path.name + ".assembled")
    with assembled.open("wb") as target:
        for source_path in ([partial] if prefix else []) + chunks:
            with source_path.open("rb") as source:
                shutil.copyfileobj(source, target, 4 * 2**20)
    if assembled.stat().st_size != size or (sha256 and digest(assembled) != sha256):
        raise OSError("Не совпал размер или SHA-256 модели после сборки")
    os.replace(assembled, path)
    for source_path in ([partial] if prefix else []) + chunks:
        source_path.unlink()


def download(url: str, path: Path, sha256: str | None, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and sha256 and digest(path) == sha256:
        return
    if size >= 256 * 2**20:
        multipart(url, path, sha256, size)
        return
    partial = path.with_name(path.name + ".partial")
    for attempt in range(3):
        start = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={start}-"} if start else {})
        try:
            with urllib.request.urlopen(request, timeout=120) as source:
                if start and source.status != 206:
                    start = 0
                if shutil.disk_usage(path.parent).free < max(0, size - start) + 5 * 2**30:
                    raise OSError("Недостаточно места: нужен размер файла + 5 ГБ запаса")
                done, reported = start, start
                with partial.open("ab" if start else "wb") as target:
                    while chunk := source.read(4 * 2**20):
                        target.write(chunk)
                        done += len(chunk)
                        if done - reported >= 256 * 2**20:
                            print(
                                f"{path.name}: {done / 2**30:.2f} / {size / 2**30:.2f} GiB",
                                flush=True,
                            )
                            reported = done
            if partial.stat().st_size != size:
                raise OSError("Неполная загрузка")
            if sha256 and digest(partial) != sha256:
                partial.unlink()
                raise OSError("Не совпала SHA-256 модели")
            os.replace(partial, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1)


def ollama(root: Path, model: str) -> dict[str, Any]:
    name, tag = model.split(":", 1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Недопустимый тег модели")
    if "/" in name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in name):
        raise ValueError("Используйте публичную модель library, например qwen3.5:27b")
    manifest, raw = read_json(f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag}")
    layers = [manifest["config"], *manifest["layers"]]
    root.mkdir(parents=True, exist_ok=True)
    needed = sum(
        layer["size"]
        for layer in layers
        if not (root / "blobs" / layer["digest"].replace(":", "-")).is_file()
    )
    if shutil.disk_usage(root).free < needed + 5 * 2**30:
        raise OSError("Не хватает места для модели и 5 ГБ запаса")
    for layer in layers:
        sha = layer["digest"].removeprefix("sha256:")
        download(
            f"https://registry.ollama.ai/v2/library/{name}/blobs/{layer['digest']}",
            root / "blobs" / ("sha256-" + sha),
            sha,
            layer["size"],
        )
    target = root / "manifests" / "registry.ollama.ai" / "library" / name / tag
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    result = {"model": model, "digest": hashlib.sha256(raw).hexdigest()}
    (root / "manifest.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    return result


# Модели faster-whisper от Systran; размер задаёт ASR_REVISION, по умолчанию small.
_ASR_MODEL = re.compile(r"(?:tiny|base|small|medium)(?:\.en)?|large-v[123]")
DEFAULT_ASR_MODEL = "small"


def asr_repository(model: str) -> str:
    if not _ASR_MODEL.fullmatch(model):
        raise ValueError(
            f"Неизвестная модель Whisper {model!r}: tiny, base, small, medium, large-v1…v3"
        )
    return f"Systran/faster-whisper-{model}"


def asr(root: Path, model: str = DEFAULT_ASR_MODEL) -> None:
    repo = asr_repository(model)
    info, _ = read_json(f"https://huggingface.co/api/models/{repo}/revision/main?blobs=true")
    revision = info["sha"]
    if not re.fullmatch(r"[0-9a-f]+", revision):
        raise ValueError("Недопустимая ревизия Whisper")

    artifacts = {}
    for item in info["siblings"]:
        name = item["rfilename"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.(json|bin|txt|md)", name):
            continue
        lfs = item.get("lfs") or {}
        size = item.get("size")
        if size is None:
            size = lfs.get("size")
        sha256 = lfs.get("sha256")
        blob_id = item.get("blobId")
        if not isinstance(size, int) or size < 0 or not (sha256 or (not lfs and blob_id)):
            raise OSError(f"Нет размера или контрольной суммы ASR: {name}")
        artifacts[name] = (size, sha256, blob_id)
    if not ASR_REQUIRED_FILES.issubset(artifacts):
        raise OSError("В репозитории ASR отсутствуют обязательные файлы")

    def manifest_for(directory: Path) -> str:
        check_asr_model(directory)
        unexpected = (
            {path.name for path in directory.iterdir()} - artifacts.keys() - {"manifest.json"}
        )
        if unexpected:
            raise OSError(f"Посторонние файлы в ревизии ASR: {', '.join(sorted(unexpected))}")
        files = {}
        for name, (size, sha256, blob_id) in artifacts.items():
            path = directory / name
            if path.is_symlink() or path.stat().st_size != size:
                raise OSError(f"Не совпал размер файла ASR: {name}")
            files[name] = digest(path)
            if sha256:
                valid = files[name] == sha256
            else:
                # Hugging Face reports the Git blob ID for regular files.
                data = path.read_bytes()
                valid = hashlib.sha1(f"blob {size}\0".encode() + data).hexdigest() == blob_id
            if not valid:
                raise OSError(f"Не совпала контрольная сумма ASR: {name}")
        return json.dumps({"repository": repo, "revision": revision, "files": files}, indent=2)

    revisions = root / "revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    if revisions.resolve() != root.resolve() / "revisions":
        raise OSError(f"Недопустимый каталог ревизий ASR: {revisions}")
    target = revisions / f"{model}-{revision}"
    with tempfile.TemporaryDirectory(prefix=".asr-", dir=revisions) as temporary:
        stage = Path(temporary) / "model"
        verify_existing = target.exists()
        if not verify_existing:
            stage.mkdir()
            for name, (size, sha256, _) in artifacts.items():
                download(
                    f"https://huggingface.co/{repo}/resolve/{revision}/{name}",
                    stage / name,
                    sha256,
                    size,
                )
            (stage / "manifest.json").write_text(manifest_for(stage))
            try:
                stage.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
                verify_existing = True
        if verify_existing and (
            target.is_symlink() or (target / "manifest.json").read_text() != manifest_for(target)
        ):
            raise OSError(f"Некорректная ревизия ASR: {target}")
        selector = Path(temporary) / ASR_SELECTOR
        selector.write_text(json.dumps(target.relative_to(root).as_posix()) + "\n")
        os.replace(selector, root / ASR_SELECTOR)
    print(f"ASR: {repo}@{revision}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["ollama", "asr", "all"], default="asr")
    parser.add_argument("--directory", type=Path, default=Path("/models/asr"))
    parser.add_argument("--model", default="qwen3.5:27b")
    parser.add_argument("--asr-model", default=os.environ.get("ASR_REVISION") or DEFAULT_ASR_MODEL)
    args = parser.parse_args()
    if args.kind == "all":
        ollama(args.directory / "ollama", args.model)
        asr(args.directory / "asr", args.asr_model)
    elif args.kind == "ollama":
        ollama(args.directory, args.model)
    else:
        asr(args.directory, args.asr_model)


if __name__ == "__main__":
    main()
