#!/usr/bin/env bash
# Режим распознавания речи для make и make doctor: печатает off, cpu или gpu.
#
# ASR_MODE берётся из окружения, затем из .env; по умолчанию auto. auto выбирает gpu, если
# в системе две GPU и больше (Ollama и Whisper на разных картах), иначе off: единственная
# карта остаётся за Ollama. Включить распознавание на машине с одной GPU — ASR_MODE=cpu.
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${ASR_MODE:-}
if [[ -z "$mode" && -f .env ]]; then
  mode=$(sed -n 's/^ASR_MODE=//p' .env | tail -n 1)
fi
mode=${mode:-auto}
case "$mode" in
  off | cpu | gpu) echo "$mode" ;;
  auto)
    gpus=0
    if command -v nvidia-smi >/dev/null; then
      gpus=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
    fi
    if (( gpus >= 2 )); then echo gpu; else echo off; fi
    ;;
  *)
    echo "ASR_MODE=$mode: допустимы auto, off, cpu, gpu" >&2
    exit 1
    ;;
esac
