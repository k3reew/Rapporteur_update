"""Deployment invariants that can be checked without a Docker daemon or GPU."""

import hashlib
from pathlib import Path

import pytest
import yaml

from rapporteur.llm.profiles import load_profile


def test_compose_has_only_one_public_port_and_independent_devices() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    assert not services["ollama"].get("ports") and not services["asr"].get("ports")
    llm = services["ollama"]["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"]
    speech = services["asr"]["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"]
    assert len(llm) == len(speech) == 1 and llm != speech
    assert ":${WEB_PORT:-18763}:8000" in services["app"]["ports"][0]
    assert compose["networks"]["internal"]["internal"] is True
    assert services["pull"]["deploy"] == {}


def test_asr_is_optional_and_cpu_variant_leaves_gpu_to_ollama() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    gpu, cpu = services["asr"], services["asr-cpu"]
    # ASR не запускается без своего профиля; приложение без него стартует.
    assert gpu["profiles"] == ["asr-gpu"] and cpu["profiles"] == ["asr-cpu"]
    for app in (services["app"], services["app-shared"]):
        assert app["depends_on"]["asr"]["required"] is False
        assert app["depends_on"]["asr-cpu"]["required"] is False
        assert app["environment"]["ASR_ENABLED"] == "${ASR_ENABLED:-0}"
    assert gpu["environment"]["ASR_DEVICE"] == "cuda"
    assert cpu["environment"]["ASR_DEVICE"] == "cpu"
    assert "reservations" not in cpu["deploy"]["resources"]
    assert cpu["networks"]["internal"]["aliases"] == ["asr"]
    assert not cpu.get("ports")


def test_server_profile_uses_actual_manifest_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifests/registry.ollama.ai/library/qwen3.5/27b"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"layers":[]}')
    monkeypatch.setenv("OLLAMA_MODELS_PATH", str(tmp_path))
    profile = load_profile("server")
    assert profile.model == "qwen3.5:27b"
    assert profile.weights_revision == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b'{"layers":[1]}')
    assert load_profile("server").weights_revision != profile.weights_revision


def test_offline_bundle_accepts_v2_manifests_and_repairs_corrupt_copy(tmp_path: Path) -> None:
    import json

    from rapporteur.bundle import export_model, manifest_path

    source, target = tmp_path / "source", tmp_path / "target"
    payload = b"checked model data"
    sha = hashlib.sha256(payload).hexdigest()
    (source / "blobs").mkdir(parents=True)
    (source / "blobs" / ("sha256-" + sha)).write_bytes(payload)
    manifest = source / "manifests-v2" / manifest_path("qwen3.5:27b")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"config": {"digest": "sha256:" + sha}, "layers": []}))
    export_model("qwen3.5:27b", source, target)
    blob = target / "blobs" / ("sha256-" + sha)
    blob.write_bytes(b"x" * len(payload))
    export_model("qwen3.5:27b", source, target)
    assert blob.read_bytes() == payload


@pytest.mark.parametrize(("device", "compute_type"), [("cpu", "int8"), ("cuda", "float16")])
def test_asr_runtime_uses_compute_type_supported_by_device(
    monkeypatch: pytest.MonkeyPatch, device: str, compute_type: str
) -> None:
    import sys
    from types import SimpleNamespace
    from typing import Any

    from rapporteur import asr_service

    captured: dict[str, Any] = {}

    def load_model(path: str, **options: Any) -> object:
        captured.update(options)
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=load_model))
    monkeypatch.setattr(asr_service, "_model", None)
    monkeypatch.setenv("ASR_DEVICE", device)
    monkeypatch.delenv("ASR_COMPUTE_TYPE", raising=False)
    assert asr_service.model() is not None
    assert captured["device"] == device
    assert captured["compute_type"] == compute_type
    assert captured["local_files_only"] is True


def test_build_package_fetch_validates_hash_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import runpy

    script = runpy.run_path("scripts/fetch-debs.py")
    fetch = script["fetch"]
    monkeypatch.setitem(
        fetch.__globals__,
        "Path",
        lambda value: tmp_path if value == "/var/cache/apt/archives" else Path(value),
    )
    payload = b"verified deb contents"
    monkeypatch.setattr(script["urllib"].request, "urlopen", lambda *a, **k: io.BytesIO(payload))
    package = (
        "https://example.test/package.deb",
        "package.deb",
        str(len(payload)),
        hashlib.sha256(payload).hexdigest(),
    )
    fetch(package)
    assert (tmp_path / "package.deb").read_bytes() == payload
    monkeypatch.setattr(
        script["urllib"].request, "urlopen", lambda *a, **k: pytest.fail("cache miss")
    )
    fetch(package)


def test_multipart_download_checks_ranges_and_assembled_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import urllib.request

    from rapporteur import offline_models

    payload = b"locked CUDA wheel" * 100

    class Response(io.BytesIO):
        status = 206

        def __init__(self, start: int, end: int) -> None:
            super().__init__(payload[start : end + 1])
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}

    def open_range(request: urllib.request.Request, timeout: int) -> Response:
        interval = str(request.get_header("Range")).removeprefix("bytes=")
        start, end = map(int, interval.split("-"))
        return Response(start, end)

    monkeypatch.setattr(urllib.request, "urlopen", open_range)
    target = tmp_path / "wheel.whl"
    offline_models.multipart(
        "https://example.test/wheel",
        target,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        chunk_bytes=400,
    )
    assert target.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [target]


def test_cuda_prefetch_uses_native_locked_wheel_and_registry_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runpy
    from typing import Any

    script = runpy.run_path("scripts/fetch-cuda-wheels.py")
    main = script["main"]
    payload = b"CUDA wheel"
    expected = hashlib.sha256(payload).hexdigest()
    wheel = {
        "url": "https://example.test/nvidia_demo-linux_x86_64.whl",
        "hash": "sha256:" + expected,
        "size": len(payload),
    }
    packages = [
        {
            "name": "nvidia-demo",
            "wheels": [wheel, dict(wheel, url="https://example.test/linux_aarch64.whl")],
        },
        {"name": "other", "wheels": [wheel]},
    ]
    monkeypatch.setattr(script["tomllib"], "loads", lambda _: {"package": packages})
    monkeypatch.setattr(script["platform"], "machine", lambda: "x86_64")
    (tmp_path / "uv.lock").touch()
    monkeypatch.setitem(main.__globals__, "Path", lambda value: tmp_path / Path(value).name)
    fetched: list[str] = []

    def fetch(url: str, path: Path, sha: str, size: int, **options: Any) -> None:
        assert sha == expected and size == len(payload)
        path.write_bytes(payload)
        fetched.append(url)

    commands: list[list[str]] = []
    monkeypatch.setitem(main.__globals__, "multipart", fetch)
    monkeypatch.setattr(script["subprocess"], "run", lambda args, **options: commands.append(args))
    main()
    main()
    assert fetched == [wheel["url"]]
    assert all("--no-installer-metadata" in command for command in commands)
