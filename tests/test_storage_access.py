import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from rapporteur.app import create_app
from rapporteur.storage import Storage
from rapporteur.storage_access import StoragePermissionError, check_storage, repair_storage


class Result(BaseModel):
    value: str


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require an unprivileged user")
def test_cache_miss_detects_readonly_directory_before_model_call(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    directory = tmp_path / "cache" / "vision"
    directory.mkdir(parents=True)
    directory.chmod(0o555)
    calls = []

    def compute() -> Result:
        calls.append(True)
        return Result(value="описание слайда")

    try:
        with pytest.raises(PermissionError, match="make storage-repair"):
            storage.cached("cache/vision/page.json", Result, compute)
        assert not calls
        assert list(directory.iterdir()) == []
    finally:
        directory.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require an unprivileged user")
def test_app_rejects_writable_root_with_readonly_nested_cache(tmp_path: Path) -> None:
    directory = tmp_path / "cache" / "vision"
    directory.mkdir(parents=True)
    directory.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="cache/vision"):
            create_app(tmp_path, process=False)
        assert not (tmp_path / "rapporteur.db").exists()
    finally:
        directory.chmod(0o755)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EROFS])
def test_failed_atomic_replace_preserves_previous_cache_without_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    storage = Storage(tmp_path)
    storage.put("cache/vision/page.json", b"previous")

    def denied(source: str, target: Path) -> None:
        raise OSError(error_number, "denied", str(target))

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(PermissionError, match="make storage-repair"):
        storage.put("cache/vision/page.json", b"new")
    assert storage.get("cache/vision/page.json") == b"previous"
    assert sorted(p.name for p in (tmp_path / "cache/vision").iterdir()) == ["page.json"]


def test_failed_cleanup_does_not_hide_original_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)

    def denied(source: str, target: Path) -> None:
        raise PermissionError(errno.EACCES, "original replace error", str(target))

    def cleanup_denied(path: Path, *, missing_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "cleanup error", str(path))

    monkeypatch.setattr(os, "replace", denied)
    monkeypatch.setattr(Path, "unlink", cleanup_denied)
    with pytest.raises(StoragePermissionError, match="make storage-repair"):
        storage.put("cache/vision/page.json", b"data")


@pytest.mark.parametrize("original_mode", [0o400, 0o4400])
def test_repair_keeps_payloads_and_does_not_change_unrelated_files(
    tmp_path: Path, original_mode: int
) -> None:
    payload = tmp_path / "cache/vision/slide.json"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"cached result")
    database = tmp_path / "rapporteur.db"
    database.write_bytes(b"database bytes")
    note = tmp_path / "administrator.txt"
    note.write_bytes(b"leave alone")
    note.chmod(0o400)
    payload.chmod(original_mode)
    payload.parent.chmod(0o555)
    database.chmod(0o400)
    repair_storage(tmp_path, os.geteuid(), os.getegid())
    check_storage(tmp_path)
    assert payload.read_bytes() == b"cached result"
    assert database.read_bytes() == b"database bytes"
    assert note.read_bytes() == b"leave alone"
    assert note.stat().st_mode & 0o777 == 0o400
    assert payload.stat().st_mode & 0o777 == 0o600
    assert payload.parent.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_repair_rejects_links_before_changing_any_permissions(tmp_path: Path, link: str) -> None:
    root = tmp_path / "data"
    cache = root / "cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not touch")
    outside.chmod(0o400)
    if link == "symbolic":
        (cache / "unsafe").symlink_to(outside)
    else:
        os.link(outside, cache / "unsafe")
    root.chmod(0o555)
    try:
        with pytest.raises(ValueError, match="ссыл"):
            repair_storage(root, os.geteuid(), os.getegid())
        assert root.stat().st_mode & 0o777 == 0o555
        assert outside.stat().st_mode & 0o777 == 0o400
        assert outside.read_bytes() == b"do not touch"
    finally:
        root.chmod(0o755)


def test_repair_refuses_unrelated_directory(tmp_path: Path) -> None:
    (tmp_path / "foreign-project").mkdir()
    with pytest.raises(ValueError, match="не похож"):
        repair_storage(tmp_path, os.geteuid(), os.getegid())


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require an unprivileged user")
def test_existing_readable_cache_hit_does_not_require_directory_write(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.put("cache/vision/page.json", Result(value="saved").model_dump_json().encode())
    directory = tmp_path / "cache/vision"
    directory.chmod(0o555)

    def compute() -> Result:
        pytest.fail("A valid cached result must not be recomputed")

    try:
        assert storage.cached("cache/vision/page.json", Result, compute).value == "saved"
    finally:
        directory.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require an unprivileged user")
def test_runtime_permission_failure_has_actionable_http_error(tmp_path: Path) -> None:
    app = create_app(tmp_path, process=False)

    @app.get("/test-storage")
    def write() -> dict[str, str]:
        app.state.storage.put("cache/vision/page.json", b"data")
        return {"status": "ok"}

    directory = tmp_path / "cache/vision"
    directory.mkdir(parents=True)
    directory.chmod(0o555)
    try:
        with TestClient(app) as client:
            response = client.get("/test-storage")
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "storage_unwritable"
        assert "make storage-repair" in body["message"]
        assert body["request_id"] == response.headers["x-request-id"]
    finally:
        directory.chmod(0o755)


@pytest.mark.parametrize("model_location", [None, "asr", "parent", "root", "override"])
def test_maintenance_uses_only_app_image_and_explicit_numeric_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_location: str | None
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy("scripts/storage-access.sh", scripts)
    shutil.copy("src/rapporteur/storage_access.py", scripts / "storage-access.py")
    storage = tmp_path / "data"
    storage.mkdir()
    environment = f"RAPPORTEUR_STORAGE={storage}\n"
    if model_location == "asr":
        environment += f"ASR_MODELS_DIR={storage}/cache/weights\n"
    elif model_location == "parent":
        environment += f"OLLAMA_MODELS_DIR={tmp_path}\n"
    elif model_location == "root":
        environment += "OLLAMA_MODELS_DIR=/\n"
    (tmp_path / ".env").write_text(environment)
    commands = tmp_path / "commands"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['COMMANDS'],'a') as log: print(json.dumps(args),file=log)\n"
        "if 'config' in args:\n"
        " print('services:\\n  ollama:\\n    image: ollama:existing"
        "\\n  app:\\n    image: rapporteur:local\\n    volumes:\\n    - type: bind"
        f"\\n      source: {storage}\\n      target: /data')\n"
        " if os.getenv('COMPOSE_OVERRIDE_MODEL'):\n"
        "  print('    - type: bind\\n      source: '+os.environ['COMPOSE_OVERRIDE_MODEL']"
        "+'\\n      target: /models/asr')\n"
        "elif args[:2]==['image','inspect'] and args[-1]!='rapporteur:local': sys.exit(1)\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("COMMANDS", str(commands))
    monkeypatch.setenv("HOST_UID", "1472")
    monkeypatch.setenv("HOST_GID", "1473")
    monkeypatch.delenv("RAPPORTEUR_STORAGE", raising=False)
    if model_location == "override":
        monkeypatch.setenv("COMPOSE_OVERRIDE_MODEL", str(storage / "cache/weights"))
    result = subprocess.run(["bash", str(scripts / "storage-access.sh"), "check"], check=False)
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    if model_location is not None:
        assert result.returncode != 0
        assert not any(args[0] == "run" for args in calls)
        return
    assert result.returncode == 0
    run = next(args for args in calls if args[0] == "run")
    assert run[run.index("--user") + 1] == "1472:1473"
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--pull") + 1] == "never"
    assert run[run.index("--entrypoint") + 1] == "python"
    assert len([arg for arg in run if arg == "--mount"]) == 2
    assert run[-4:] == ["rapporteur:local", "/storage-access.py", "check", "/data"]


def test_live_preflight_ignores_a_disappearing_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "tmp/upload"
    directory.mkdir(parents=True)
    temporary = directory / "staged"
    temporary.write_bytes(b"transient")
    original = Path.is_dir

    def vanished(path: Path) -> bool:
        if path == temporary:
            temporary.unlink(missing_ok=True)
        return original(path)

    monkeypatch.setattr(Path, "is_dir", vanished)
    check_storage(tmp_path)
    assert not temporary.exists()


def test_repair_rejects_nested_mount_before_touching_model_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "cache/mounted-model/weights.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    model.chmod(0o400)
    original = Path.read_text

    def mountinfo(path: Path, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/self/mountinfo":
            return f"42 21 8:1 / {model.parent} rw - ext4 /dev/fake rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    with pytest.raises(ValueError, match="монтир"):
        repair_storage(tmp_path, os.geteuid(), os.getegid())
    assert model.stat().st_mode & 0o777 == 0o400


def test_repair_does_not_follow_an_ancestor_replaced_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    source = root / "cache/vision/page.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"cached")
    external = tmp_path / "external/vision"
    external.mkdir(parents=True)
    external.chmod(0o555)
    original = os.open
    swapped = False

    def swap(path: str | Path, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(path).name == "vision" and not swapped:
            swapped = True
            (root / "cache").rename(root / "saved-cache")
            (root / "cache").symlink_to(external.parent)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    try:
        with pytest.raises(OSError):
            repair_storage(root, os.geteuid(), os.getegid())
        assert swapped
        assert external.stat().st_mode & 0o777 == 0o555
    finally:
        external.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="Permission checks require an unprivileged user")
def test_real_upload_route_reports_denied_temporary_directory(tmp_path: Path) -> None:
    from .workflow_support import seed_regulation

    app = create_app(tmp_path, process=False)
    seed_regulation(app.state.database)
    directory = tmp_path / "tmp"
    directory.mkdir()
    with TestClient(app, raise_server_exceptions=False) as client:
        conference = client.post("/api/v1/conferences", json={}).json()["id"]
        directory.chmod(0o555)
        try:
            response = client.post(
                f"/api/v1/conferences/{conference}/materials",
                params={"path": "speech.txt", "purpose": "document"},
                content=b"sample",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 503
            assert response.json()["code"] == "storage_unwritable"
        finally:
            directory.chmod(0o755)
