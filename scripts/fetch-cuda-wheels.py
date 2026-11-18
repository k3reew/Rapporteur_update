"""Prefetch large locked CUDA wheels using checked HTTP ranges."""

import platform
import subprocess
import tomllib
from pathlib import Path

from rapporteur.offline_models import digest, multipart


def main() -> None:
    packages = tomllib.loads(Path("uv.lock").read_text())["package"]
    directory = Path("/tmp/cuda-wheels")
    directory.mkdir(exist_ok=True)
    wheels = []
    for package in packages:
        if not package["name"].startswith("nvidia-"):
            continue
        for wheel in package.get("wheels", []):
            filename = wheel["url"].rsplit("/", 1)[1]
            if platform.machine() not in filename or "linux" not in filename:
                continue
            path = Path(directory) / filename
            if not path.is_file() or digest(path) != wheel["hash"].removeprefix("sha256:"):
                multipart(
                    wheel["url"],
                    path,
                    wheel["hash"].removeprefix("sha256:"),
                    wheel["size"],
                    chunk_bytes=8 * 2**20,
                )
            wheels.append(str(path))
    if wheels:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                "/opt/venv/bin/python",
                "--no-deps",
                "--no-installer-metadata",
                *wheels,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
