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
    monkeypatch.setattr(asr_service, "_model_directory", None)
    assert asr_service.model(Path("/models/asr")) is not None
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


@pytest.mark.parametrize("failure", ["download", "checksum", "activate", None])
def test_asr_download_preserves_active_files_until_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    import json
    import os

    from rapporteur import offline_models

    existing = {"model.bin": b"old model", "config.json": b"old config", "notes.txt": b"admin"}
    for name, data in existing.items():
        (tmp_path / name).write_bytes(data)
    old_revision = tmp_path / "revisions/large-v3-abc"
    old_revision.mkdir(parents=True)
    (old_revision / "model.bin").write_bytes(b"previously selected model")
    old_pointer = json.dumps("revisions/large-v3-abc")
    (tmp_path / "active.json").write_text(old_pointer)
    requested: list[str] = []
    downloaded: list[str] = []
    names = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    payload = b"ok"

    def read_json(url: str) -> tuple[dict[str, object], bytes]:
        requested.append(url)
        return {
            "sha": "abc",
            "siblings": [
                {
                    "rfilename": name,
                    "size": len(payload),
                    **(
                        {"lfs": {"sha256": hashlib.sha256(payload).hexdigest()}}
                        if name == "model.bin"
                        else {"blobId": hashlib.sha1(b"blob 2\0" + payload).hexdigest()}
                    ),
                }
                for name in names
            ],
        }, b""

    def download(url: str, path: Path, sha256: str | None, size: int) -> None:
        for name, data in existing.items():
            assert (tmp_path / name).read_bytes() == data
        if path.name == "model.bin" and failure == "download":
            raise OSError("broken connection")
        downloaded.append(path.name)
        path.write_bytes(b"no" if path.name == "model.bin" and failure == "checksum" else payload)

    monkeypatch.setattr(offline_models, "read_json", read_json)
    monkeypatch.setattr(offline_models, "download", download)
    replace = os.replace

    def replace_model(source: Path, target: Path) -> None:
        if failure == "activate" and target.name == "active.json":
            raise OSError("activation failed")
        replace(source, target)

    monkeypatch.setattr(os, "replace", replace_model)
    if failure:
        with pytest.raises(OSError):
            offline_models.asr(tmp_path, "small")
        assert (tmp_path / "active.json").read_text() == old_pointer
    else:
        offline_models.asr(tmp_path, "small")
        from rapporteur.asr_models import resolve_asr_model

        active = resolve_asr_model(tmp_path)
        assert active.parent == tmp_path / "revisions"
        manifest = json.loads((active / "manifest.json").read_text())
        assert manifest["repository"] == "Systran/faster-whisper-small"
        assert set(manifest["files"]) == set(names)
        offline_models.asr(tmp_path, "small")
        assert downloaded == names
        assert resolve_asr_model(tmp_path) == active

        (active / "preprocessor_config.json").write_text('{"feature_size":128}')
        (tmp_path / "active.json").write_text(old_pointer)
        with pytest.raises(OSError, match=r"preprocessor_config.json"):
            offline_models.asr(tmp_path, "small")
        assert (tmp_path / "active.json").read_text() == old_pointer

    assert "Systran/faster-whisper-small" in requested[0]
    for name, data in existing.items():
        assert (tmp_path / name).read_bytes() == data
    assert (old_revision / "model.bin").read_bytes() == b"previously selected model"
    assert not list(tmp_path.rglob(".asr-*"))
    with pytest.raises(ValueError):
        offline_models.asr_repository("../../evil")


def test_asr_health_follows_selector_and_validates_selected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from fastapi import HTTPException

    from rapporteur import asr_service
    from rapporteur.asr_models import ASR_REQUIRED_FILES, resolve_asr_model

    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    for name in ASR_REQUIRED_FILES:
        (tmp_path / name).write_bytes(b"legacy")
    assert resolve_asr_model(tmp_path) == tmp_path
    assert asr_service.health() == {"status": "ok"}

    selector = tmp_path / "active.json"
    active = tmp_path / "revisions/small-123"
    active.mkdir(parents=True)
    selector.write_text(json.dumps("revisions/small-123"))
    with pytest.raises(HTTPException) as incomplete:
        asr_service.health()
    assert incomplete.value.status_code == 503
    for name in ASR_REQUIRED_FILES:
        (active / name).write_bytes(b"new")
    assert asr_service.health() == {"status": "ok"}

    (active.parent / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    for invalid in ("revisions/escape", "revisions/../small-123", str(active)):
        selector.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            resolve_asr_model(tmp_path)


def test_asr_reports_revision_of_loaded_model_after_selector_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import subprocess
    import sys
    from types import SimpleNamespace

    from rapporteur import asr_service
    from rapporteur.asr_models import ASR_REQUIRED_FILES
    from rapporteur.storage import Storage, blob_key

    selector = tmp_path / "active.json"
    storage = Storage(tmp_path / "data")
    sha = "a" * 64
    storage.put(blob_key(sha), b"dummy audio")
    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(asr_service, "Storage", lambda _: storage)
    monkeypatch.setattr(asr_service, "_model", None)
    monkeypatch.setattr(asr_service, "_model_directory", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b'{"format":{"duration":1}}'),
    )
    loaded: list[str] = []

    def load_model(directory: str, **options: object) -> SimpleNamespace:
        loaded.append(directory)
        return SimpleNamespace(transcribe=lambda *args, **kwargs: ([], None))

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=load_model))
    for version in ("old", "new"):
        directory = tmp_path / "revisions" / version
        directory.mkdir(parents=True)
        selector.write_text(json.dumps(f"revisions/{version}"))
        for name in ASR_REQUIRED_FILES:
            (directory / name).write_bytes(b"model")
        (directory / "manifest.json").write_text(version)
        assert asr_service.transcribe(asr_service.Input(sha256=sha))["revision"] == "old"

    assert loaded == [str(tmp_path / "revisions/old")]


@pytest.mark.parametrize("shared", [False, True])
def test_make_asr_mode_stops_inactive_profiles_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
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
    shutil.copy("scripts/asr-mode.sh", scripts)
    (scripts / "doctor.sh").write_text('echo doctor >> "$COMMANDS"\nexit "${DOCTOR_STATUS:-0}"\n')
    shutil.copy("Makefile", tmp_path)
    commands = tmp_path / "commands"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['COMMANDS'], 'a') as log:\n"
        "    print(json.dumps([os.getenv('ASR_ENABLED'), *sys.argv[1:]]), file=log)\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("COMMANDS", str(commands))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("OLLAMA_NETWORK", "external" if shared else "")
    for target, mode, inactive, active in [
        ("up", "gpu", ["asr-cpu"], "asr"),
        ("start", "cpu", ["asr"], "asr-cpu"),
        ("start", "off", ["asr", "asr-cpu"], None),
        ("up", "cpu", ["asr"], "asr-cpu"),
        ("up", "cpu", ["asr"], "asr-cpu"),
    ]:
        commands.write_text("")
        subprocess.run(["make", target, f"ASR_MODE={mode}"], check=True, cwd=tmp_path)
        doctor, stopped, started = commands.read_text().splitlines()
        assert doctor == "doctor"
        stop_args = json.loads(stopped)
        assert "--profile" in stop_args
        assert "asr-gpu" in stop_args and "asr-cpu" in stop_args
        assert stop_args[stop_args.index("stop") + 1 :] == inactive
        start_args = json.loads(started)
        assert start_args[0] == ("1" if active else "0")
        assert ("app-shared" in start_args) is shared
        assert ("--build" in start_args) is (target == "up")
        for variant in ("asr-cpu", "asr-gpu"):
            assert (variant in start_args) is (variant == f"asr-{mode}")

    commands.write_text("")
    monkeypatch.setenv("DOCTOR_STATUS", "1")
    assert subprocess.run(["make", "up"], cwd=tmp_path).returncode
    assert commands.read_text() == "doctor\n"


def test_bundle_rebuild_replaces_asr_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os
    import shutil
    import subprocess

    from rapporteur.asr_models import ASR_REQUIRED_FILES, resolve_asr_model

    if shutil.which("make") is None:
        pytest.skip("GNU Make is required to build the offline bundle")

    for name in ("Makefile", "compose.yaml", ".env.example"):
        shutil.copy(name, tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    for name in ("doctor.sh", "asr-mode.sh"):
        shutil.copy(Path("scripts") / name, tmp_path / "scripts")
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$*" in "compose config --images ollama") echo ollama:fake ;; esac\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    destination = tmp_path / "bundle/models/asr"
    for model in ("a", "b", "legacy"):
        source = tmp_path / f"models-{model}"
        revision = source if model == "legacy" else source / "revisions" / model
        revision.mkdir(parents=True)
        if model != "legacy":
            (source / "active.json").write_text(json.dumps(f"revisions/{model}"))
        for name in ASR_REQUIRED_FILES:
            (revision / name).write_text(model)
        subprocess.run(["make", "bundle", f"ASR_MODELS_DIR={source}"], cwd=tmp_path, check=True)
        assert (resolve_asr_model(destination) / "model.bin").read_text() == model
        assert sorted(p.relative_to(destination) for p in destination.rglob("*")) == sorted(
            p.relative_to(source) for p in source.rglob("*")
        )
        subprocess.run(
            ["sha256sum", "--check", "--quiet", "SHA256SUMS"], cwd=tmp_path / "bundle", check=True
        )

    assert subprocess.run(
        ["make", "bundle", f"ASR_MODELS_DIR={tmp_path / 'missing'}"], cwd=tmp_path
    ).returncode
    subprocess.run(["make", "bundle", f"ASR_MODELS_DIR={destination}"], cwd=tmp_path, check=True)
    assert (destination / "model.bin").read_text() == "legacy"


def test_bundle_contains_current_release_instructions_and_checksums() -> None:
    makefile = Path("Makefile").read_text()
    assert "cp -a docs/. $(BUNDLE)/docs/" in makefile
    checksum_command = next(line for line in makefile.splitlines() if "xargs -0 sha256sum" in line)
    assert "docs" in checksum_command
    assert "docs/benchmarks/2026-11-17-release-0.3.0.md" in makefile
