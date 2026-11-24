# Своя и общая Ollama

## Содержание документа

<!-- codebase-analysis-ai:scope:start -->
Режимы подключения Ollama, требования к весам и выбору профиля. Раздел объясняет взаимодействие собственной модели, общего сервиса и отдельного ASR.
<!-- codebase-analysis-ai:scope:end -->

## Контекст

Стандартный запуск создаёт Ollama в частной сети проекта. Приложение также может обращаться к Ollama другого Docker-проекта через внешнюю сеть. В обоих случаях app получает выбранный профиль и доступ к нужным локальным манифестам.

## Справочные сведения

### Собственная Ollama

`make up` или `make start` запускает app, ollama и ASR по `ASR_MODE` (asr на GPU, asr-cpu на процессоре или без распознавания). App обращается к `http://ollama:11434`. Порт 11434 сервиса не публикуется на хосте, поэтому порт соседнего проекта не конфликтует с ним. Веса читаются из каталога `OLLAMA_MODELS_DIR`.

Для загрузки дополнительной модели на машине подготовки используется команда `make pull MODEL=qwen3.5:9b`. В каталоге должны появиться blobs и манифест нужного тега. Запуск на сервере использует имеющиеся веса.

### Общая Ollama

Узнайте Docker-сеть и сетевое имя существующего сервиса.

```bash
docker ps --format '{{.Names}}	{{.Networks}}'
```

Укажите значения в `.env`.

```dotenv
OLLAMA_NETWORK=existing_project_network
OLLAMA_URL=http://existing-ollama:11434
OLLAMA_MODELS_DIR=/srv/models/ollama
RAPPORTEUR_PROFILE=server
```

Значения сети и адреса являются примерами. `OLLAMA_URL` должен разрешаться из указанной Docker-сети. Makefile выбирает `app-shared` и сервис ASR по `ASR_MODE`. Подключение собственной Ollama в этом режиме не входит в список запускаемых сервисов.

Общий сервис должен иметь установленную модель базового профиля или сохранённого выбора. App-shared сохраняет монтирование локальных каталогов. Профиль выбирается среди моделей, манифесты которых видны через `OLLAMA_MODELS_PATH`, поэтому корректный `OLLAMA_MODELS_DIR` нужен и в этом режиме: без него server требует манифест `qwen3.5:27b`, а при отсутствии модели приложение берёт профиль с имеющейся. ASR использует свой каталог и отдельное устройство.

После изменения выполните `make down`, `make config`, `make doctor`, затем `make start` для загруженных образов. Проверьте состояние и выполните короткий сквозной запрос.

### Выбор и отказ

В настройках приложение читает `/api/tags`. Сохранение проверяет наличие выбранной модели и её возможность рассуждения. При недоступной Ollama сохранение нового выбора возвращает 503. Сброс выбора возвращает базовый профиль.

Для работы с изображениями модель должна принимать изображения. Выбор произвольной текстовой модели может завершить визуальный этап ошибкой. Наличие модели в списке подтверждает установку, а поддержка нужной модальности проверяется сквозным сценарием.

Для общей GPU учитывается конкуренция с соседними задачами. Выбор UUID и лимиты контейнеров определяют размещение, а доступная память зависит от фактически запущенных процессов. Параллельность Ollama и контекст следует согласовать с администратором общей службы.

## Связанные документы

- [Приоритет профилей](configuration.md)
- [Запуск и диагностика](offline.md)

## Источники

- Профиль shared и монтирование. [compose.yaml](../compose.yaml)
- Выбор RUN по OLLAMA_NETWORK. [Makefile](../Makefile)
- Чтение локального манифеста. [src/rapporteur/llm/profiles.py](../src/rapporteur/llm/profiles.py)
- Список и сохранение модели. [src/rapporteur/model_choice.py](../src/rapporteur/model_choice.py)
- Запросы к Ollama. [src/rapporteur/llm/client.py](../src/rapporteur/llm/client.py)
## Ollama на хосте Docker Desktop

Запустите Ollama на компьютере с Docker Desktop. Профиль `local` по умолчанию
использует `qwen3.5:9b`. Для пробного запуска с меньшей моделью выполните
`ollama pull qwen3.5:4b`, затем выберите её в «Настройках» приложения.

В PowerShell из каталога исходников выполните команды. Порт 18764 и имя проекта выделены
для локальной проверки. Материалы и база пишутся в каталог `storage` внутри этой копии.

```powershell
$env:INFERENCE_ENGINE = 'ollama'
$env:OLLAMA_HOST_URL = 'http://host.docker.internal:11434'
$env:RAPPORTEUR_PROFILE = 'local'
$env:RAPPORTEUR_OLLAMA_APP_IMAGE = 'rapporteur:0.5.0-ollama-local'
$env:WEB_PORT = '18764'
New-Item -ItemType Directory -Path storage -Force | Out-Null
docker compose -p rapporteur-ollama-local --profile host-ollama up --build -d --wait app-ollama-host
```

Откройте [страницу приложения](http://127.0.0.1:18764), затем «Настройки». Там появится
статус Ollama и список установленных моделей. Если сервис недоступен, убедитесь, что
`ollama list` работает на хосте. Адрес в `OLLAMA_HOST_URL` должен быть доступен из
контейнера. Docker Desktop предоставляет для этого имя
[`host.docker.internal`](https://docs.docker.com/desktop/features/networking/networking-how-tos/#connect-a-container-to-a-service-on-the-host).

Для запуска через GNU Make задайте те же параметры в `.env`, добавьте
`COMPOSE_PROJECT_NAME=rapporteur-ollama-local` и выполните `make up`. При
`INFERENCE_ENGINE=ollama` непустой `OLLAMA_HOST_URL` выбирает Ollama на хосте.
Без него `OLLAMA_NETWORK` выбирает Ollama в общей сети Docker, а без обоих параметров
запускается Ollama из Compose. Для серверных режимов vLLM укажите `INFERENCE_ENGINE=vllm`.
