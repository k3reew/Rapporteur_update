#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo 'Нет .env: выполните cp .env.example .env'; exit 1; }
command -v docker >/dev/null || { echo 'Установите Docker Engine и плагин Compose'; exit 1; }
docker compose config --quiet
# Parse only values we own; never execute .env as a shell script.
value() {
  local key=$1 found
  found=${!key:-}
  if [[ -z "$found" ]]; then found=$(sed -n "s/^$key=//p" .env | tail -n 1); fi
  printf '%s' "${found:-$2}"
}
port=$(value WEB_PORT 18763)
bind=$(value WEB_BIND_ADDRESS 127.0.0.1)
llm=$(value LLM_GPU_ID 0)
engine=$(value INFERENCE_ENGINE ollama)
[[ "$engine" == ollama || "$engine" == vllm ]] || { echo 'INFERENCE_ENGINE: укажите vllm или ollama'; exit 1; }
host_url=$(value OLLAMA_HOST_URL '')
models=''
if [[ "$engine" == vllm || -z "$host_url" ]]; then
  command -v nvidia-smi >/dev/null || { echo 'Установите драйвер NVIDIA'; exit 1; }
fi
if [[ "$engine" == vllm ]]; then
  [[ "$(uname -m)" == x86_64 ]] || { echo 'Для поставки vLLM нужен сервер linux/amd64'; exit 1; }
  mode=$(value VLLM_MODE bf16)
  gpu_ids=("$(value VLLM_GPU_ID_0 0)")
  case "$mode" in
    bf16)
      models=$(value VLLM_MODELS_DIR ./models/vllm/qwen3.5-27b)
      revision=81a287ca484a5e71148f5beb7f367bb5286d70f9
      gpu_ids+=("$(value VLLM_GPU_ID_1 1)")
      ;;
    gptq-single|gptq-dual)
      models=$(value VLLM_GPTQ_MODELS_DIR ./models/vllm/qwen3.5-27b-gptq-int4)
      revision=8f0c09f227ae570e79617c6d9172b59df9c16081
      if [[ "$mode" == gptq-dual ]]; then
        gpu_ids+=("$(value VLLM_GPU_ID_1 1)")
      fi
      ;;
    *)
      echo 'VLLM_MODE: укажите bf16, gptq-single или gptq-dual'; exit 1
      ;;
  esac
  slots=$(value RAPPORTEUR_INFERENCE_SLOTS 2)
  [[ "$slots" == 1 || "$slots" == 2 ]] || { echo 'RAPPORTEUR_INFERENCE_SLOTS: укажите 1 или 2'; exit 1; }
  gpu_uuids=()
  for id in "${gpu_ids[@]}"; do
    gpu=$(nvidia-smi --id="$id" --query-gpu=uuid,memory.total,driver_version,name,memory.free --format=csv,noheader,nounits) || {
      echo "GPU $id недоступна"; exit 1
    }
    IFS=, read -r uuid memory driver name free <<< "$gpu"
    for previous in "${gpu_uuids[@]}"; do
      [[ "$uuid" != "$previous" ]] || { echo 'Укажите UUID двух разных GPU'; exit 1; }
    done
    gpu_uuids+=("$uuid")
    memory=${memory// /}; driver=${driver// /}; free=${free// /}
    [[ "$memory" =~ ^[0-9]+$ && "$memory" -ge 39000 ]] || {
      echo "GPU $uuid: для этого профиля требуется 40 ГБ VRAM"; exit 1
    }
    major=${driver%%.*}
    [[ "$major" =~ ^[0-9]+$ && "$major" -ge 580 ]] || {
      echo "GPU $uuid: для образа CUDA 13 нужен драйвер NVIDIA R580 или новее"; exit 1
    }
    printf 'GPU %s:%s, свободно %s MiB из %s MiB, драйвер %s\n' "$uuid" "$name" "$free" "$memory" "$driver"
  done
  python3 - "$models" "$revision" <<'PY_CHECKPOINT'
import json
import sys
from pathlib import Path

models, revision = Path(sys.argv[1]), sys.argv[2]
try:
    manifest = json.loads((models / "manifest.json").read_text())
    if manifest["revision"] != revision:
        raise ValueError("ревизия модели не совпадает с профилем")
    required = {"config.json", "tokenizer.json", "model.safetensors.index.json", "preprocessor_config.json"}
    index = json.loads((models / "model.safetensors.index.json").read_text())
    required.update(index["weight_map"].values())
    if len(required) == 4:
        raise ValueError("пустой индекс весов")
    for name in required:
        if Path(name).name != name or name not in manifest["files"] or (models / name).stat().st_size == 0:
            raise ValueError(f"файл отсутствует или пуст: {name}")
except (OSError, ValueError, KeyError, TypeError) as error:
    raise SystemExit(f"Неполный снимок vLLM в {models}: {error}") from error
PY_CHECKPOINT
elif [[ -z "$host_url" ]]; then
  nvidia-smi --id="$llm" --query-gpu=uuid,name,memory.free --format=csv
  models=$(value OLLAMA_MODELS_DIR ./models/ollama)
  profile=$(value RAPPORTEUR_PROFILE server)
  if [[ "$profile" == server && ! -f "$models/manifests/registry.ollama.ai/library/qwen3.5/27b" && ! -f "$models/manifests-v2/registry.ollama.ai/library/qwen3.5/27b" ]]; then
    echo 'Модель qwen3.5:27b отсутствует: фактический профиль виден в «Настройках»'
  fi
  [[ -d "$models/blobs" ]] || { echo "Нет весов Ollama в $models"; exit 1; }
  [[ -d "$models/manifests" || -d "$models/manifests-v2" ]] || { echo 'Нет манифестов Ollama'; exit 1; }
fi
if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -qE ":${port}$"; then
  if ! docker compose --profile shared-ollama --profile host-ollama --profile vllm --profile vllm-gptq-single --profile vllm-gptq-dual ps --status running --format '{{.Service}}' | grep -qE '^app(-shared|-ollama-host|-vllm)?$'; then
    echo "Порт $bind:$port занят: измените WEB_PORT в .env"; exit 1
  fi
fi
storage=${RAPPORTEUR_STORAGE:-$(value RAPPORTEUR_STORAGE ./storage)}
storage_path=$(realpath -m -- "$storage")
if [[ -n "$models" ]]; then
  models_path=$(realpath -m -- "$models")
  if [[ "$storage_path" == "$models_path" || "$storage_path" == "${models_path%/}/"* || "$models_path" == "${storage_path%/}/"* ]]; then
    echo "Каталоги моделей и хранилища пересекаются: $models_path и $storage_path"; exit 1
  fi
fi
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
