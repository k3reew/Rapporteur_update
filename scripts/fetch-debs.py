"""Prefetch apt's verified package set concurrently during the image build."""

import hashlib
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def fetch(package: tuple[str, str, str, str]) -> None:
    url, filename, size, expected = package
    algorithm = "sha256"
    destination = Path("/var/cache/apt/archives") / Path(filename).name
    if destination.is_file():
        with destination.open("rb") as source:
            if hashlib.file_digest(source, algorithm).hexdigest() == expected:
                return
    partial = destination.with_suffix(".partial")
    for attempt in range(3):
        try:
            digest = hashlib.new(algorithm)
            with urllib.request.urlopen(url, timeout=90) as source, partial.open("wb") as target:
                while chunk := source.read(2**20):
                    target.write(chunk)
                    digest.update(chunk)
            if partial.stat().st_size != int(size) or digest.hexdigest() != expected:
                raise OSError(f"Package checksum mismatch: {filename}")
            partial.replace(destination)
            print(f"Downloaded {filename}", flush=True)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def main() -> None:
    Path("/var/cache/apt/archives").mkdir(parents=True, exist_ok=True)
    command = ["apt-get", "-y", "--print-uris", "--no-install-recommends", "install", *sys.argv[1:]]
    output = subprocess.check_output(command, text=True)
    packages = [shlex.split(line)[:3] for line in output.splitlines() if line.startswith("'")]
    specs = []
    for _, filename, _ in packages:
        name, version, _ = filename.rsplit("_", 2)
        specs.append(name + "=" + urllib.parse.unquote(version))
    metadata = subprocess.check_output(
        ["apt-cache", "show", "--no-all-versions", *specs], text=True
    )
    checksums = {}
    for paragraph in metadata.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line)
        if "SHA256" in fields:
            checksums[(fields["Package"], fields["Version"], fields["Architecture"])] = fields[
                "SHA256"
            ]
    verified = []
    for url, filename, size in packages:
        name, version, architecture = filename.rsplit("_", 2)
        key = (name, urllib.parse.unquote(version), architecture.removesuffix(".deb"))
        verified.append((url, filename, size, checksums[key]))
    with ThreadPoolExecutor(max_workers=12) as workers:
        list(workers.map(fetch, verified))


if __name__ == "__main__":
    main()
