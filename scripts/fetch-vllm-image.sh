#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
PYTHONPATH="$root/src" python3 "$root/scripts/fetch-vllm-image.py" "${1:?Укажите каталог поставки}"
