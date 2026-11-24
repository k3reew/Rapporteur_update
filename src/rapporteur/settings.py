"""Пути к конфигурации и настройки из окружения (spec §8.2)."""

import os
from pathlib import Path

# Корень репозитория или образа: рядом лежат prompts/, templates/, profiles.yaml и summary.yaml.
ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "prompts"
TEMPLATES_DIR = ROOT / "templates"
PROFILES_FILE = ROOT / "profiles.yaml"
SUMMARY_FILE = ROOT / "summary.yaml"

# Том с базой SQLite, оригиналами, кэшем шагов и файлами справок.
DATA_DIR = Path(os.environ.get("RAPPORTEUR_DATA_DIR", "/data"))
# Собранный фронтенд; без него приложение отдаёт только API.
STATIC_DIR = os.environ.get("RAPPORTEUR_STATIC_DIR")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000")
VLLM_SECOND_URL = os.environ.get("VLLM_SECOND_URL", "")


def vllm_endpoints() -> tuple[str, ...]:
    return (VLLM_URL, VLLM_SECOND_URL) if VLLM_SECOND_URL else (VLLM_URL,)
