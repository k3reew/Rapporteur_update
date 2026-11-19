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
# off, cpu или gpu; на GPU распознавание работает только на отдельной карте.
asr_mode=$(bash scripts/asr-mode.sh)
if [[ "$asr_mode" == gpu ]]; then
  asr=$(value ASR_GPU_ID 1)
  llm_uuid=$(nvidia-smi --id="$llm" --query-gpu=uuid --format=csv,noheader) || { echo "Нет GPU LLM_GPU_ID=$llm"; exit 1; }
  asr_uuid=$(nvidia-smi --id="$asr" --query-gpu=uuid --format=csv,noheader) || { echo "Нет GPU ASR_GPU_ID=$asr; при одной GPU задайте ASR_MODE=cpu или off"; exit 1; }
  [[ "$llm_uuid" != "$asr_uuid" ]] || { echo 'LLM_GPU_ID и ASR_GPU_ID должны указывать разные GPU; при одной GPU задайте ASR_MODE=cpu или off'; exit 1; }
  nvidia-smi --id="$llm,$asr" --query-gpu=uuid,name,memory.free --format=csv
else
  nvidia-smi --id="$llm" --query-gpu=uuid,name,memory.free --format=csv
fi
case "$asr_mode" in
  gpu) echo "Распознавание речи: GPU $asr" ;;
  cpu) echo 'Распознавание речи: процессор' ;;
  off) echo 'Распознавание речи выключено (ASR_MODE): записи не расшифровываются' ;;
esac
models=$(value OLLAMA_MODELS_DIR ./models/ollama)
speech=${ASR_MODELS_DIR:-$(value ASR_MODELS_DIR ./models/asr)}
profile=$(value RAPPORTEUR_PROFILE server)
# Модели профиля нет — приложение возьмёт профиль с имеющейся моделью (llm/profiles.py).
if [[ "$profile" == server && ! -f "$models/manifests/registry.ollama.ai/library/qwen3.5/27b" && ! -f "$models/manifests-v2/registry.ollama.ai/library/qwen3.5/27b" ]]; then
  echo 'Модели qwen3.5:27b профиля server нет в каталоге: приложение возьмёт профиль с имеющейся моделью, см. «Управление» и журнал app. Для 27b: make prepare'
fi
[[ -d "$models/blobs" ]] || { echo "Нет весов Ollama в $models: выполните make prepare на машине с интернетом"; exit 1; }
[[ -d "$models/manifests" || -d "$models/manifests-v2" ]] || { echo 'Нет манифестов Ollama'; exit 1; }
if [[ "$asr_mode" != off ]]; then
  selector="$speech/active.json"
  if [[ -e "$selector" || -L "$selector" ]]; then
    selected=$(cat "$selector")
    pointer='^[[:space:]]*"(revisions/[A-Za-z0-9._-]+)"[[:space:]]*$'
    if [[ ! "$selected" =~ $pointer ]]; then
      echo "Неверный указатель ASR: $selector"; exit 1
    fi
    revision=${BASH_REMATCH[1]}
    [[ "$(realpath -e -- "$speech/$revision")" == "$(realpath -e -- "$speech")/$revision" ]] || {
      echo "Недоступна ревизия ASR: $speech/$revision"; exit 1
    }
    speech="$speech/$revision"
  fi
  for artifact in model.bin tokenizer.json config.json; do
    [[ -f "$speech/$artifact" && -s "$speech/$artifact" ]] || {
      echo "Неполные веса ASR в $speech: выполните make prepare или задайте ASR_MODE=off"; exit 1
    }
  done
fi
if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -qE ":${port}$"; then
  if ! docker compose --profile shared-ollama ps --status running --format '{{.Service}}' | grep -qE '^app(-shared)?$'; then
    echo "Порт $bind:$port занят: измените WEB_PORT в .env"; exit 1
  fi
fi
storage=${RAPPORTEUR_STORAGE:-$(value RAPPORTEUR_STORAGE ./storage)}
parent="$storage"; [[ -d "$parent" ]] || parent=$(dirname "$parent")
[[ -w "$parent" ]] || { echo "Нет права записи в $parent"; exit 1; }
if [[ -d "$storage" ]]; then
  if bash scripts/storage-access.sh check; then
    :
  else
    status=$?
    if [[ "$status" == 3 ]]; then
      echo 'Образ app ещё не загружен: вложенные права будут проверены при запуске приложения'
    else
      exit "$status"
    fi
  fi
fi
available=$(df -Pk "$parent" | awk 'NR==2 {print $4}')
(( available >= 5242880 )) || { echo 'Для запуска нужно не менее 5 ГБ свободного места'; exit 1; }
echo "Проверка пройдена. Интерфейс: http://$bind:$port"
