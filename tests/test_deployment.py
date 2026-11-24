"""Deployment invariants that can be checked without a Docker daemon or GPU."""

import hashlib
from pathlib import Path

import pytest
import yaml

from rapporteur.llm.profiles import load_profile


def test_compose_keeps_inference_internal_and_has_no_speech_services() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    assert {"asr", "asr-cpu", "download-asr"}.isdisjoint(services)
    assert not services["ollama"].get("ports")
    assert ":${WEB_PORT:-18763}:8000" in services["app"]["ports"][0]
    assert compose["networks"]["internal"]["internal"] is True
    assert services["pull"]["deploy"] == {}


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
    offsets = []

    class Response(io.BytesIO):
        status = 206

        def __init__(self, start: int, end: int) -> None:
            super().__init__(payload[start : end + 1])
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}

    def open_range(request: urllib.request.Request, timeout: int) -> Response:
        interval = str(request.get_header("Range")).removeprefix("bytes=")
        start, end = map(int, interval.split("-"))
        offsets.append(start)
        return Response(start, end)

    monkeypatch.setattr(urllib.request, "urlopen", open_range)
    target = tmp_path / "wheel.whl"
    target.with_suffix(".whl.chunk-0").write_bytes(payload[:100])
    offline_models.multipart(
        "https://example.test/wheel",
        target,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        chunk_bytes=400,
    )
    assert target.read_bytes() == payload
    assert min(offsets) == 100
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("bf16", ["vllm"]),
        ("gptq-single", ["vllm-gptq"]),
        ("gptq-dual", ["vllm-gptq", "vllm-gptq-second"]),
    ],
)
def test_make_start_selects_only_the_requested_gpu_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: list[str]
) -> None:
    import json
    import os
    import shutil
    import subprocess
    import sys

    if shutil.which("make") is None:
        pytest.skip("GNU Make is required to exercise the host launch commands")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "doctor.sh").write_text('echo doctor >> "$COMMANDS"\n')
    shutil.copy("Makefile", tmp_path)
    commands = tmp_path / "commands"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['COMMANDS'], 'a') as log:\n"
        "    print(json.dumps(sys.argv[1:]), file=log)\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("COMMANDS", str(commands))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    subprocess.run(
        ["make", "start", "INFERENCE_ENGINE=vllm", f"VLLM_MODE={mode}"],
        check=True,
        cwd=tmp_path,
    )
    lines = commands.read_text().splitlines()
    assert lines[0] == "doctor"
    started = json.loads(lines[-1])
    assert started[started.index("up") + 1 : started.index("--detach")] == ["app-vllm", *expected]
    assert "--no-build" in started and "--pull" in started and "never" in started


def test_bundle_contains_current_release_instructions_and_checksums() -> None:
    makefile = Path("Makefile").read_text()
    assert "cp -a docs/. $(BUNDLE)/docs/" in makefile
    checksum_command = next(line for line in makefile.splitlines() if "xargs -0 sha256sum" in line)
    assert "docs" in checksum_command
    assert "docs/benchmarks/2026-11-17-release-0.3.0.md" in makefile


def test_vllm_is_internal_and_app_can_start_during_model_load() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    services = compose["services"]
    vllm = services["vllm"]
    app = services["app-vllm"]
    ids = vllm["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"]
    assert ids == ["${VLLM_GPU_ID_0:-0}", "${VLLM_GPU_ID_1:-1}"]
    assert vllm["networks"] == ["internal"] and "ports" not in vllm
    assert "vllm" not in app.get("depends_on", {})
    assert app["environment"]["VLLM_URL"] == "${VLLM_URL:-http://vllm:8000}"
    assert vllm["environment"]["HF_HUB_OFFLINE"] == "1"
    assert vllm["environment"]["VLLM_NO_USAGE_STATS"] == "1"
    assert "--tensor-parallel-size=2" in vllm["command"]
    assert vllm["volumes"][0]["read_only"] is True


def test_gptq_replicas_each_bind_one_gpu_and_share_readonly_weights() -> None:
    services = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    first, second = (services[name] for name in ("vllm-gptq", "vllm-gptq-second"))
    for service in (first, second):
        assert "--tensor-parallel-size=1" in service["command"]
        assert "--max-num-seqs=1" in service["command"]
        assert "--quantization=gptq_marlin" in service["command"]
        assert len(service["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"]) == 1
        assert service["volumes"][0]["read_only"] and "ports" not in service
    assert first["deploy"] != second["deploy"]
    assert first["volumes"] == second["volumes"]
    assert first["profiles"] == ["vllm-gptq-single", "vllm-gptq-dual"]
    assert second["profiles"] == ["vllm-gptq-dual"]
