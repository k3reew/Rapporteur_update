#!/usr/bin/env bash
# Запускается на сервере от администратора отдельной тестовой установки.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
url=${1:?Укажите URL тестовой установки на 127.0.0.1}
out=${2:?Укажите новый каталог протокола}
mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"
out=$(realpath -- "$out")
nvidia-smi > "$out/nvidia-smi.txt"
{
  nvidia-smi topo -m
  nvidia-smi topo -p2p r || :
  nvidia-smi topo -p2p w || :
} > "$out/topology.txt" 2>&1
nvidia-smi --query-gpu=timestamp,uuid,utilization.gpu,memory.used,power.draw \
  --format=csv -l 1 > "$out/gpu.csv" &
monitor=$!
finish() {
  kill "$monitor" 2>/dev/null || :
  wait "$monitor" 2>/dev/null || :
  (cd "$root" && make acceptance-logs) > "$out/services.log" 2>&1 || :
  grep -E 'stage=(vision|screening|summary)( |$)' "$out/services.log" > "$out/stages.txt" || :
}
trap finish EXIT
python3 "$root/scripts/acceptance-vllm.py" "$url" "$out"
