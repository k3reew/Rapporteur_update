"""Resolve the active offline Whisper revision, including legacy model directories."""

import json
import re
from pathlib import Path

ASR_REQUIRED_FILES = frozenset({"config.json", "model.bin", "tokenizer.json"})
ASR_SELECTOR = "active.json"


def resolve_asr_model(root: Path) -> Path:
    selector = root / ASR_SELECTOR
    try:
        selected = json.loads(selector.read_text())
    except FileNotFoundError:
        if not selector.is_symlink():
            return root
        raise ValueError(f"Недоступен указатель ASR: {selector}") from None
    if not isinstance(selected, str) or not re.fullmatch(r"revisions/[A-Za-z0-9._-]+", selected):
        raise ValueError(f"Неверный указатель ASR: {selector}")
    directory = root / selected
    if directory.resolve() != root.resolve() / selected or not directory.is_dir():
        raise ValueError(f"Недоступна ревизия ASR: {directory}")
    return directory


def check_asr_model(directory: Path) -> None:
    for name in sorted(ASR_REQUIRED_FILES):
        path = directory / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Неполные веса ASR: {path}")
