#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:?Укажите каталог поставки}
bf16="$out/models/vllm/qwen3.5-27b"
gptq="$out/models/vllm/qwen3.5-27b-gptq-int4"
[[ -s "$out/vllm-image.tar" && -s "$bf16/manifest.json" && -s "$gptq/manifest.json" ]] || {
  echo "В $out отсутствуют проверенные модели или образ vLLM. Выполните make prepare-vllm." >&2
  exit 1
}
[[ ! -e "$out/models/asr" ]] || {
  echo 'Укажите отдельный каталог для 0.5.0. В выбранном каталоге находится прежняя поставка.' >&2
  exit 1
}
for model in "$bf16" "$gptq"; do
  if [[ -n "$(find "$model" -type f \( -name '*.partial' -o -name '*.chunk-*' -o -name '*.assembled' \) -print -quit)" ]]; then
    echo "В $model остались временные файлы загрузки. Завершите подготовку моделей." >&2
    exit 1
  fi
done
if [[ "${2:-}" != --no-build ]]; then
  docker compose --profile vllm build app-vllm
fi
docker image inspect rapporteur:0.5.0 >/dev/null
docker save rapporteur:0.5.0 | gzip -1 > "$out/images.tar.gz.partial"
mv -- "$out/images.tar.gz.partial" "$out/images.tar.gz"
mkdir -p -- "$out/scripts" "$out/docs/examples" "$out/licenses"
cp compose.yaml Makefile "$out/"
cp scripts/{doctor,storage-access,acceptance-vllm}.sh "$out/scripts/"
cp scripts/{acceptance-vllm,compare-acceptance}.py "$out/scripts/"
cp src/rapporteur/storage_access.py "$out/scripts/storage-access.py"
cp docs/{acceptance-0.5.0,workflow-0.5.0}.md "$out/docs/"
cp -a docs/examples/acceptance-0.5.0 "$out/docs/examples/"
cp docs/licenses.md "$out/licenses/COMPONENTS.md"
cp docs/licenses/{UPSTREAM-0.5.0.md,vLLM-Apache-2.0.txt} "$out/licenses/"
cp "$bf16/LICENSE" "$out/licenses/Qwen3.5-27B-BF16.txt"
cp "$gptq/LICENSE" "$out/licenses/Qwen3.5-27B-GPTQ-Int4.txt"
sed -e 's/](acceptance-0.5.0.md)/](docs\/acceptance-0.5.0.md)/' \
    -e 's/](workflow-0.5.0.md)/](docs\/workflow-0.5.0.md)/' docs/release-0.5.0.md > "$out/README.md"
{
  cat .env.example
  printf '\n# Параметры автономной поставки.\n'
  printf '%s\n' COMPOSE_PROJECT_NAME=rapporteur-v050 VLLM_IMAGE=vllm/vllm-openai:v0.32.0
  printf '%s\n' RAPPORTEUR_OLLAMA_APP_IMAGE=rapporteur:0.5.0
} > "$out/.env.example"
(
  cd "$out"
  artifacts=(images.tar.gz vllm-image.tar models/vllm compose.yaml Makefile .env.example README.md scripts docs licenses)
  if [[ -f VALIDATION.md ]]; then artifacts+=(VALIDATION.md); fi
  find "${artifacts[@]}" -type f -print0 |
    LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS.partial
  mv -- SHA256SUMS.partial SHA256SUMS
)
du -sh "$out"
