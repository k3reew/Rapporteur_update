"""Download pinned Qwen snapshots on the connected preparation host."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapporteur.offline_models import digest, download, read_json

QWEN_REVISION = "81a287ca484a5e71148f5beb7f367bb5286d70f9"
GPTQ_REVISION = "8f0c09f227ae570e79617c6d9172b59df9c16081"


def fetch_snapshot(repository: str, revision: str, target: Path) -> None:
    info, _ = read_json(
        f"https://huggingface.co/api/models/{repository}/revision/{revision}?blobs=true"
    )
    if info["sha"] != revision:
        raise OSError(f"Не совпала ревизия {repository}")
    target.mkdir(parents=True, exist_ok=True)

    def fetch(file: dict) -> tuple[str, str]:
        name = file["rfilename"]
        if Path(name).name != name or name.startswith("."):
            raise ValueError(f"Недопустимое имя: {name}")
        size = file["size"]
        lfs = file.get("lfs") or {}
        sha256 = lfs.get("sha256")
        if lfs and not sha256:
            raise ValueError(f"Нет SHA-256 LFS: {name}")
        blob_id = file["blobId"]
        path = target / name

        def valid_git_blob() -> bool:
            if not path.is_file() or path.stat().st_size != size:
                return False
            raw = path.read_bytes()
            return hashlib.sha1(f"blob {size}\0".encode() + raw).hexdigest() == blob_id

        try:
            if sha256 or not valid_git_blob():
                download(
                    f"https://huggingface.co/{repository}/resolve/{revision}/{name}",
                    path,
                    sha256,
                    size,
                )
            if not sha256 and not valid_git_blob():
                raise OSError(f"Не совпал хеш Git-файла: {name}")
        except Exception as error:
            raise OSError(f"{repository}/{name}: {error}") from error
        return name, sha256 or digest(path)

    with ThreadPoolExecutor(max_workers=12) as pool:
        files = dict(
            pool.map(
                fetch, [row for row in info["siblings"] if not row["rfilename"].startswith(".")]
            )
        )
    manifest = target / "manifest.json"
    temporary = manifest.with_suffix(".partial")
    temporary.write_text(
        json.dumps({"repository": repository, "revision": revision, "files": files}, indent=2)
        + "\n"
    )
    temporary.replace(manifest)
    print(f"Проверена модель {repository}@{revision}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--model", choices=["all", "bf16", "gptq"], default="all")
    args = parser.parse_args()
    models = args.bundle / "models/vllm"
    if args.model in ("all", "bf16"):
        fetch_snapshot("Qwen/Qwen3.5-27B", QWEN_REVISION, models / "qwen3.5-27b")
    if args.model in ("all", "gptq"):
        fetch_snapshot(
            "Qwen/Qwen3.5-27B-GPTQ-Int4", GPTQ_REVISION, models / "qwen3.5-27b-gptq-int4"
        )


if __name__ == "__main__":
    main()
