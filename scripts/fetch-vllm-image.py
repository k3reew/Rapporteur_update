"""Fetch the pinned linux/amd64 image without extracting its layers into Docker."""

import argparse
import hashlib
import io
import json
import re
import shutil
import tarfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rapporteur.offline_models import digest, download

REPOSITORY = "vllm/vllm-openai"
TAG = f"{REPOSITORY}:v0.32.0"
REGISTRY = f"https://registry-1.docker.io/v2/{REPOSITORY}"
MANIFEST = "sha256:6ee133356d49ed24cee03dd0cda3d3980279a11c107ce3e752c89e76540f41c9"


class RegistryAuth(urllib.request.BaseHandler):
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.token = ""
        self.expires_at = 0.0

    def https_request(self, request: urllib.request.Request) -> urllib.request.Request:
        if request.full_url.startswith(REGISTRY + "/"):
            with self.lock:
                if time.monotonic() >= self.expires_at:
                    url = (
                        "https://auth.docker.io/token?service=registry.docker.io"
                        f"&scope=repository:{REPOSITORY}:pull"
                    )
                    with urllib.request.urlopen(url, timeout=30) as response:
                        answer = json.load(response)
                    self.token = answer["token"]
                    self.expires_at = time.monotonic() + max(1, int(answer["expires_in"]) - 60)
                request.add_unredirected_header("Authorization", "Bearer " + self.token)
        return request


def seed_downloads(old: Path, cache: Path, layers: dict[str, int]) -> None:
    if not old.is_file():
        return
    with tarfile.open(old, "r:") as archive:
        try:
            for member in archive:
                name = member.name.removesuffix(".tar.gz").removeprefix("sha256:")
                if name not in layers or member.size != layers[name]:
                    continue
                target = cache / name
                if target.exists():
                    continue
                partial = target.with_suffix(".partial")
                if partial.exists():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source, partial.open("wb") as output:
                    shutil.copyfileobj(source, output, 2**20)
                if digest(partial) == name:
                    partial.replace(target)
                else:
                    partial.unlink()
        except tarfile.ReadError:
            # The previous exporter can end in the middle of a layer. Saved bytes are a prefix.
            pass


def image_manifest() -> dict[str, Any]:
    request = urllib.request.Request(
        f"{REGISTRY}/manifests/{MANIFEST}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    with urllib.request.urlopen(request, timeout=60) as answer:
        raw = answer.read()
    if "sha256:" + hashlib.sha256(raw).hexdigest() != MANIFEST:
        raise OSError("Не совпал digest манифеста образа vLLM")
    return json.loads(raw)


def export(out: Path, cache: Path) -> None:
    urllib.request.install_opener(urllib.request.build_opener(RegistryAuth()))
    manifest = image_manifest()
    config = manifest["config"]["digest"].removeprefix("sha256:")
    layer_names = [entry["digest"].removeprefix("sha256:") for entry in manifest["layers"]]
    artifacts = {
        entry["digest"].removeprefix("sha256:"): entry["size"]
        for entry in [manifest["config"], *manifest["layers"]]
    }
    if not all(re.fullmatch(r"[a-f0-9]{64}", name) for name in artifacts):
        raise ValueError("Недопустимые digest слоёв")
    cache.mkdir(parents=True, exist_ok=True)
    partial = out / "vllm-image.tar.partial"
    seed = cache / "previous-export.tar"
    if partial.is_file() and not seed.exists():
        partial.replace(seed)
    seed_downloads(seed, cache, artifacts)

    def fetch(item: tuple[str, int]) -> None:
        name, size = item
        download(f"{REGISTRY}/blobs/sha256:{name}", cache / name, name, size)
        print(f"vLLM: sha256:{name} проверен", flush=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, sorted(artifacts.items(), key=lambda item: -item[1])))
    descriptor = {
        "Config": config + ".json",
        "RepoTags": [TAG],
        "Layers": [layer + ".tar.gz" for layer in layer_names],
    }
    record = json.dumps([descriptor], separators=(",", ":")).encode()
    with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as archive:
        archive.add(cache / config, arcname=descriptor["Config"])
        for name in dict.fromkeys(layer_names):
            archive.add(cache / name, arcname=name + ".tar.gz")
        member = tarfile.TarInfo("manifest.json")
        member.size = len(record)
        archive.addfile(member, io.BytesIO(record))
    partial.replace(out / "vllm-image.tar")
    print(f"Проверен и экспортирован {TAG}@{MANIFEST}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    args.bundle.mkdir(parents=True, exist_ok=True)
    if not (args.bundle / "vllm-image.tar").is_file():
        export(args.bundle, args.bundle.with_name(f".{args.bundle.name}-image"))
