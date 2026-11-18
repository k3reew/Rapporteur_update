#!/usr/bin/env bash
# Runs with the same numeric identity as make up/start, using an already loaded image.
set -euo pipefail
cd "$(dirname "$0")/.."
action=${1:-check}
[[ "$action" == check || "$action" == repair ]] || { echo 'Ожидается check или repair' >&2; exit 1; }
[[ -f .env ]] || { echo 'Нет .env' >&2; exit 1; }
value() { local found; found=$(sed -n "s/^$1=//p" .env | tail -n 1); printf '%s' "${found:-$2}"; }
export HOST_UID=${HOST_UID:-$(id -u)} HOST_GID=${HOST_GID:-$(id -g)}
tool=src/rapporteur/storage_access.py
[[ -f "$tool" ]] || tool=scripts/storage-access.py
[[ -f "$tool" ]] || { echo 'Нет инструмента storage-access.py в исходниках или комплекте' >&2; exit 1; }
tool=$(realpath -e -- "$tool")
# Normalized Compose includes overrides; --images also lists dependency images.
application=$(docker compose config --format yaml app | awk '
  $0 == "  app:" { service=1; next }
  service && /^  [^ ]/ { service=0 }
  service { print }
')
image=$(printf '%s\n' "$application" | sed -n 's/^    image: //p')
[[ -n "$image" ]] || { echo 'Не найден образ сервиса app в Compose' >&2; exit 1; }
bind_source() {
  printf '%s\n' "$application" | awk -v target="$1" '
    /^[[:space:]]*- type:/ { kind=$3; source="" }
    /^[[:space:]]*source:/ { source=$0; sub(/^[[:space:]]*source: /,"",source) }
    /^[[:space:]]*target:/ && $2==target && kind=="bind" { print source }
  '
}
storage=$(bind_source /data)
[[ -n "$storage" && -d "$storage" ]] || { echo "Не найден bind-каталог /data: $storage" >&2; exit 1; }
storage=$(realpath -e -- "$storage")
models=("${OLLAMA_MODELS_DIR:-$(value OLLAMA_MODELS_DIR ./models/ollama)}"
        "${ASR_MODELS_DIR:-$(value ASR_MODELS_DIR ./models/asr)}")
for target in /models/ollama /models/asr; do
  source=$(bind_source "$target")
  [[ -z "$source" ]] || models+=("$source")
done
for model in "${models[@]}"; do
  model=$(realpath -m -- "$model")
  if [[ "$model" == "$storage" || "$model" == "${storage%/}/"* || "$storage" == "${model%/}/"* ]]; then
    echo "Каталоги моделей и хранилища пересекаются: $model и $storage; исправление запрещено" >&2
    exit 1
  fi
done
docker image inspect "$image" >/dev/null 2>&1 || { echo "Нет локального образа $image: сначала загрузите образы комплекта" >&2; exit 3; }
user="$HOST_UID:$HOST_GID"
arguments=(check /data)
if [[ "$action" == repair ]]; then
  user=0:0
  arguments=(repair /data --uid "$HOST_UID" --gid "$HOST_GID")
fi
docker run --rm --pull never --network none --user "$user" \
  --mount "type=bind,src=$storage,dst=/data" \
  --mount "type=bind,src=$tool,dst=/storage-access.py,readonly" \
  --entrypoint python "$image" /storage-access.py "${arguments[@]}"
