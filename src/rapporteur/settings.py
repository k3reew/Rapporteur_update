"""Пути к конфигурации и настройки из окружения (spec §8.2)."""

import os
from pathlib import Path

# Корень репозитория или образа: рядом лежат prompts/, templates/ и profiles.yaml.
ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "prompts"
TEMPLATES_DIR = ROOT / "templates"
PROFILES_FILE = ROOT / "profiles.yaml"

# Том с базой SQLite, оригиналами, кэшем шагов и файлами справок.
DATA_DIR = Path(os.environ.get("RAPPORTEUR_DATA_DIR", "/data"))
# Собранный фронтенд; без него приложение отдаёт только API.
STATIC_DIR = os.environ.get("RAPPORTEUR_STATIC_DIR")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
ASR_URL = os.environ.get("ASR_URL", "http://asr:18764")
# Запущен ли ASR (make передаёт по ASR_MODE). Без него записи пропускаются с предупреждением.
ASR_ENABLED = os.environ.get("ASR_ENABLED", "1") != "0"
