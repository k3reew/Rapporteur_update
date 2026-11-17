#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo 'Нет .env: выполните cp .env.example .env'; exit 1; }
command -v docker >/dev/null || { echo 'Установите Docker Engine и плагин Compose'; exit 1; }
command -v nvidia-smi >/dev/null || { echo 'Установите драйвер NVIDIA'; exit 1; }
docker compose config --quiet
# Parse only values we own; never execute .env as a shell script.
value() { local found; found=$(sed -n "s/^$1=//p" .env | tail -n 1); printf '%s' "${found:-$2}"; }
port=$(value WEB_PORT 18763)
bind=$(value WEB_BIND_ADDRESS 127.0.0.1)
llm=$(value LLM_GPU_ID 0)
asr=$(value ASR_GPU_ID 1)
llm_uuid=$(nvidia-smi --id="$llm" --query-gpu=uuid --format=csv,noheader)
asr_uuid=$(nvidia-smi --id="$asr" --query-gpu=uuid --format=csv,noheader)
[[ "$llm_uuid" != "$asr_uuid" ]] || { echo 'LLM_GPU_ID и ASR_GPU_ID должны указывать разные GPU'; exit 1; }
nvidia-smi --id="$llm,$asr" --query-gpu=uuid,name,memory.free --format=csv
models=$(value OLLAMA_MODELS_DIR ./models/ollama)
speech=$(value ASR_MODELS_DIR ./models/asr)
profile=$(value RAPPORTEUR_PROFILE server)
if [[ "$profile" == server && ! -f "$models/manifests/registry.ollama.ai/library/qwen3.5/27b" && ! -f "$models/manifests-v2/registry.ollama.ai/library/qwen3.5/27b" ]]; then
  echo 'Для профиля server нужен полный каталог qwen3.5:27b: выполните make prepare'; exit 1
fi
[[ -d "$models/blobs" ]] || { echo "Нет весов Ollama в $models: выполните make prepare на машине с интернетом"; exit 1; }
[[ -d "$models/manifests" || -d "$models/manifests-v2" ]] || { echo 'Нет манифестов Ollama'; exit 1; }
[[ -f "$speech/model.bin" && -f "$speech/tokenizer.json" && -f "$speech/config.json" && -f "$speech/preprocessor_config.json" ]] || { echo "Неполные веса ASR в $speech"; exit 1; }
if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -qE ":${port}$"; then
  if ! docker compose ps --status running --format '{{.Service}}' | grep -qE '^app(-shared)?$'; then
    echo "Порт $bind:$port занят: измените WEB_PORT в .env"; exit 1
  fi
fi
storage=$(value RAPPORTEUR_STORAGE ./storage)
parent="$storage"; [[ -d "$parent" ]] || parent=$(dirname "$parent")
[[ -w "$parent" ]] || { echo "Нет права записи в $parent"; exit 1; }
available=$(df -Pk "$parent" | awk 'NR==2 {print $4}')
(( available >= 5242880 )) || { echo 'Для запуска нужно не менее 5 ГБ свободного места'; exit 1; }
echo "Проверка пройдена. Интерфейс: http://$bind:$port"
