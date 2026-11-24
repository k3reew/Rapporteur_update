# Все команды идут через Docker Compose: на машине нужны только Docker и make.
# CI нет (ADR-011): make check запускается перед каждым слиянием в main.
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

COMPOSE := docker compose
DEV := $(COMPOSE) run --rm --build dev
WEB := $(COMPOSE) run --rm --build web-dev
PYTHON_IMAGE := python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
NODE_IMAGE := node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da
# Только версии npm старше двух недель на момент блокировки; дату сдвигать вместе с make lock.
NPM_BEFORE := 2026-10-20
# Образ Ollama из compose.yaml: по digest на машине сборки, в комплекте — тегом без digest.
OLLAMA_PINNED = $(shell $(COMPOSE) config --images ollama)
OLLAMA_TAG = $(firstword $(subst @, ,$(OLLAMA_PINNED)))
BUNDLE ?= bundle
INFERENCE_ENGINE ?= $(or $(shell sed -n 's/^INFERENCE_ENGINE=//p' .env 2>/dev/null),ollama)
export INFERENCE_ENGINE
VLLM_MODE ?= $(or $(shell sed -n 's/^VLLM_MODE=//p' .env 2>/dev/null),bf16)
export VLLM_MODE
MODEL ?= qwen3.5:27b
# Ollama другого проекта: при указанной сети используется app-shared.
OLLAMA_NETWORK ?= $(shell sed -n 's/^OLLAMA_NETWORK=//p' .env 2>/dev/null)
OLLAMA_HOST_URL ?= $(shell sed -n 's/^OLLAMA_HOST_URL=//p' .env 2>/dev/null)
export OLLAMA_HOST_URL
RAPPORTEUR_STORAGE ?= $(or $(shell sed -n 's/^RAPPORTEUR_STORAGE=//p' .env 2>/dev/null),./storage)
ALL := $(COMPOSE) --profile shared-ollama --profile host-ollama --profile vllm --profile vllm-gptq-single --profile vllm-gptq-dual
RUNTIME_SERVICES := app app-shared app-ollama-host app-vllm ollama vllm vllm-gptq vllm-gptq-second
ifeq ($(INFERENCE_ENGINE),vllm)
WAIT_TIMEOUT := 420
ifeq ($(VLLM_MODE),bf16)
VLLM_PROFILE := vllm
ACTIVE_SERVICES := app-vllm vllm
else
VLLM_PROFILE := vllm-$(VLLM_MODE)
ACTIVE_SERVICES := app-vllm vllm-gptq
export VLLM_APP_PROFILE := server-vllm-gptq
export VLLM_URL := http://vllm-gptq:8000
ifeq ($(VLLM_MODE),gptq-dual)
ACTIVE_SERVICES += vllm-gptq-second
export VLLM_SECOND_URL := http://vllm-gptq-second:8000
else
override export RAPPORTEUR_INFERENCE_SLOTS := 1
endif
endif
RUN = $(COMPOSE) --profile $(VLLM_PROFILE)
else ifneq ($(strip $(OLLAMA_HOST_URL)),)
RUN = $(COMPOSE) --profile host-ollama
ACTIVE_SERVICES := app-ollama-host
else ifneq ($(strip $(OLLAMA_NETWORK)),)
RUN = $(COMPOSE) --profile shared-ollama
ACTIVE_SERVICES := app-shared
else
RUN = $(COMPOSE)
ACTIVE_SERVICES := app ollama
endif
WAIT_TIMEOUT ?= 180

.DEFAULT_GOAL := help
.PHONY: bundle-docs help up start down ps logs config check lint test format pull lock bundle install prepare prepare-vllm bundle-vllm doctor storage-check storage-repair acceptance-logs

help:
	@grep -E '^## ' Makefile | sed 's/^## //'

## make up      — собрать приложение и запустить выбранный профиль, http://127.0.0.1:18763
## make start   — запустить без сборки и скачивания образов (машина без интернета)
up start:
	@bash scripts/doctor.sh
	mkdir -p "$(RAPPORTEUR_STORAGE)"
	$(ALL) stop $(filter-out $(ACTIVE_SERVICES),$(RUNTIME_SERVICES))
	$(RUN) up $(ACTIVE_SERVICES) --detach --wait --wait-timeout $(WAIT_TIMEOUT) $(if $(filter up,$@),--build,--no-build --pull never)

## make down    — остановить; база, материалы и справки остаются в storage/
down:
	$(ALL) down

## make ps      — состояние сервисов
ps:
	$(ALL) ps

## make logs    — журнал приложения и локальной модели
logs:
	$(ALL) logs --follow

acceptance-logs:
	@$(ALL) logs --no-color --timestamps $(ACTIVE_SERVICES)

## make config  — проверить compose.yaml и .env без запуска
config:
	$(ALL) config --quiet

## make check   — линтеры, типы, тесты и сборка фронтенда в контейнерах
check: lint test

lint:
	$(DEV) sh -c 'ruff check . && ruff format --check . && mypy'
	$(WEB) sh -c 'eslint . && tsc -p tsconfig.json'

test:
	$(DEV) pytest
	$(WEB) sh -c 'vitest run && vite build --outDir /tmp/dist --emptyOutDir'

## make format  — поправить стиль Python
format:
	$(DEV) sh -c 'ruff check --fix . && ruff format .'

## make pull MODEL=qwen3.5:9b — скачать модель в OLLAMA_MODELS_DIR (нужен интернет)
pull:
	@test -n "$(MODEL)" || { echo "укажите MODEL" >&2; exit 2; }
	$(COMPOSE) run --rm pull $(MODEL)

## make lock    — обновить uv.lock и package-lock.json после правки зависимостей
lock:
	docker run --rm --user $(HOST_UID):$(HOST_GID) -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache \
	  -v $(CURDIR):/app -w /app $(PYTHON_IMAGE) \
	  sh -c 'pip install --quiet --no-cache-dir --user uv==0.13.7 && /tmp/.local/bin/uv lock'
	docker run --rm --user $(HOST_UID):$(HOST_GID) -e HOME=/tmp \
	  -v $(CURDIR)/frontend:/app -w /app $(NODE_IMAGE) \
	  npm install --package-lock-only --ignore-scripts --no-audit --no-fund --before=$(NPM_BEFORE)

## make bundle [MODEL="qwen3.5:9b …"] — комплект прежнего профиля Ollama в bundle/
## По умолчанию в комплект включается серверная qwen3.5:27b (docs/offline.md).
bundle:
	$(COMPOSE) build app
	docker pull $(OLLAMA_PINNED)
	docker tag $(OLLAMA_PINNED) $(OLLAMA_TAG)
	mkdir -p $(BUNDLE)/models
	$(COMPOSE) run --rm export-model $(MODEL)
	bash -o pipefail -c 'docker save rapporteur:local $(OLLAMA_TAG) | gzip -1 > $(BUNDLE)/images.tar.gz.partial'
	mv $(BUNDLE)/images.tar.gz.partial $(BUNDLE)/images.tar.gz
	cp compose.yaml Makefile $(BUNDLE)/
	mkdir -p $(BUNDLE)/scripts
	cp scripts/doctor.sh $(BUNDLE)/scripts/
	cp scripts/storage-access.sh $(BUNDLE)/scripts/
	cp src/rapporteur/storage_access.py $(BUNDLE)/scripts/storage-access.py
	$(MAKE) bundle-docs
	sed 's|^INFERENCE_ENGINE=.*|INFERENCE_ENGINE=ollama|; s|^OLLAMA_MODELS_DIR=.*|OLLAMA_MODELS_DIR=./models|' .env.example > $(BUNDLE)/.env.example
	printf '\n# Образ Ollama из комплекта: тег вместо digest (docs/offline.md).\nOLLAMA_IMAGE=%s\n' \
	  "$(OLLAMA_TAG)" >> $(BUNDLE)/.env.example
	cd $(BUNDLE) && find images.tar.gz models compose.yaml Makefile .env.example README.md WORKFLOW.md ACCEPTANCE.md VALIDATION.md scripts docs -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
	@du -sh $(BUNDLE)

## make bundle-docs — обновить инструкции внутри подготовленного комплекта
bundle-docs:
	mkdir -p $(BUNDLE)/docs
	cp -a docs/. $(BUNDLE)/docs/
	printf '# Офлайн-установка «Репортёра»\n\n[Перенос, запуск и обновление](docs/offline.md)\n\n[Выпуск 0.3.0 и резервное копирование](docs/release-0.3.0.md)\n' > $(BUNDLE)/README.md
	printf '# Руководство оператора\n\n[Открыть руководство](docs/workflow.md)\n' > $(BUNDLE)/WORKFLOW.md
	printf '# Серверная приёмка\n\n[Открыть порядок проверки](docs/acceptance.md)\n' > $(BUNDLE)/ACCEPTANCE.md
	printf '# Локальная проверка 0.3.0\n\n[Протокол](docs/benchmarks/2026-11-17-release-0.3.0.md)\n' > $(BUNDLE)/VALIDATION.md

## make install — в каталоге комплекта: проверить контрольные суммы и загрузить образы
install:
	sha256sum --check --quiet SHA256SUMS
	docker load --input images.tar.gz
	@if [ -f vllm-image.tar ]; then docker load --input vllm-image.tar; fi
	@test -f .env || { cp .env.example .env; echo "Создан .env: проверьте WEB_BIND_ADDRESS и WEB_PORT"; }

## make prepare — подготовить прежний профиль Ollama на машине с интернетом
prepare:
	@mkdir -p $$(sed -n 's/^OLLAMA_MODELS_DIR=//p' .env)
	$(COMPOSE) build app
	$(MAKE) pull MODEL=$(MODEL)

## make prepare-vllm BUNDLE=/path — скачать закреплённые модели и образ vLLM на машине с интернетом
prepare-vllm:
	PYTHONPATH=src python3 scripts/fetch-vllm-models.py "$(BUNDLE)"
	bash scripts/fetch-vllm-image.sh "$(BUNDLE)"

## make bundle-vllm BUNDLE=/path — собрать полный офлайн-комплект после prepare-vllm
bundle-vllm:
	bash scripts/bundle-vllm.sh "$(BUNDLE)"

## make doctor — проверить настройки, порт, GPU и наличие моделей без изменения сервера
doctor:
	@bash scripts/doctor.sh

## make storage-check — проверить права вложенных каталогов от UID/GID приложения без обращения к моделям
storage-check:
	@bash scripts/storage-access.sh check

## make storage-repair — остановить писателей, исправить владельца/права только хранилища и проверить доступ
storage-repair:
	$(ALL) stop app app-shared app-ollama-host app-vllm
	@bash scripts/storage-access.sh repair
	@bash scripts/storage-access.sh check
