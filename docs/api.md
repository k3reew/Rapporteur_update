# Справочник HTTP API

## Содержание документа

<!-- codebase-analysis-ai:scope:start -->
Полный API приложения и внутреннего сервиса распознавания. Описаны входные структуры, ответы, ошибки, версии и пример последовательности запросов.
<!-- codebase-analysis-ai:scope:end -->

## Контекст

Базовый префикс предметных методов — `/api/v1`. Схема приложения доступна через `GET /openapi.json`. Файлы экспортированной схемы приложены к этому справочнику и могут использоваться офлайн.

HTTP 202 означает принятие фоновой работы. Готовность проверяется отдельным GET. Номер версии нужно читать у редактируемого объекта перед изменением. Примеры адресованы доверенному клиенту приложения.

## Методы и пути

Все пути приложения получены из `create_app(..., process=False).openapi()`. Схема содержит 30 адресов и 45 операций, включая `/health`.

| Метод и путь | Действие | Тело | Успех |
| --- | --- | --- | --- |
| `GET /health` | Доступность процесса | `-` | 200 |
| `GET /api/v1/conferences` | Список конференций | `-` | 200 |
| `POST /api/v1/conferences` | Создать конференцию | `ConferenceWrite` | 201 |
| `GET /api/v1/conferences/{conference_id}` | Карточка конференции | `-` | 200 |
| `PUT /api/v1/conferences/{conference_id}` | Сохранить метаданные конференции | `ConferenceWrite` | 200 |
| `DELETE /api/v1/conferences/{conference_id}` | Удалить конференцию | `-` | 204 |
| `POST /api/v1/conferences/{conference_id}/materials` | Загрузить оригинал | `application/octet-stream` | 201 |
| `GET /api/v1/conferences/{conference_id}/materials` | Список материалов, привязок и реестра | `-` | 200 |
| `PUT /api/v1/conferences/{conference_id}/materials/bindings` | Подтвердить целые привязки | `BindingReview` | 200 |
| `GET /api/v1/conferences/{conference_id}/conference.yaml` | Выгрузить conference.yaml | `-` | 200 |
| `GET /api/v1/files/{file_id}/content` | Открыть оригинал | `-` | 200 |
| `GET /api/v1/files/{file_id}/pages/{page_no}` | PNG подготовленной страницы | `-` | 200 |
| `GET /api/v1/files/{file_id}/slides/{slide_no}` | PNG слайда | `-` | 200 |
| `PUT /api/v1/files/{file_id}/purpose` | Изменить назначение | `PurposeWrite` | 204 фактически, 200 в OpenAPI |
| `POST /api/v1/conferences/{conference_id}/talks` | Добавить ручной доклад | `TalkCreate` | 201 |
| `PUT /api/v1/talks/{talk_id}` | Изменить тему и авторов | `TalkUpdate` | 204 |
| `GET /api/v1/talks/{talk_id}` | Метаданные и ревизия доклада | `-` | 200 |
| `POST /api/v1/conferences/{conference_id}/processing` | Запустить обработку | `-` | 202 |
| `GET /api/v1/conferences/{conference_id}/progress` | Прогресс и разрешения проверки | `-` | 200 |
| `POST /api/v1/talks/{talk_id}/retry` | Повторить обработку доклада | `-` | 202 |
| `GET /api/v1/conferences/{conference_id}/preparation` | Прочитать предложение | `-` | 200 |
| `POST /api/v1/conferences/{conference_id}/preparation` | Поставить подготовку в очередь | `-` | 202 |
| `PUT /api/v1/conferences/{conference_id}/preparation` | Подтвердить предложение | `ProposalWrite` | 200 |
| `GET /api/v1/talks/{talk_id}/summary` | Сводка, пакет и статус | `-` | 200 |
| `PUT /api/v1/talks/{talk_id}/summary` | Сохранить правку сводки | `SummaryEdit` | 200 |
| `POST /api/v1/talks/{talk_id}/summary/review` | Подтвердить сводку | `SummaryReview` | 200 |
| `POST /api/v1/conferences/{conference_id}/summaries/review` | Массовое подтверждение | `BulkReview` | 200 |
| `GET /api/v1/departments` | Актуальные версии отделов | `-` | 200 |
| `POST /api/v1/departments` | Загрузить положение | `application/octet-stream` | 201 |
| `GET /api/v1/talks/{talk_id}/recommendations` | Прочитать рекомендации | `-` | 200 |
| `POST /api/v1/talks/{talk_id}/recommendations` | Рассчитать рекомендации | `-` | 202 |
| `PUT /api/v1/talks/{talk_id}/recommendations` | Подтвердить рекомендации | `Review` | 200 |
| `GET /api/v1/conferences/{conference_id}/reports` | Список версий справки | `-` | 200 |
| `POST /api/v1/conferences/{conference_id}/reports` | Создать версию справки | `ReportBuild` | 201 |
| `GET /api/v1/report-versions/{version_id}/files/{report_format}` | Скачать файл версии | `-` | 200 |
| `GET /api/v1/system` | Состояние установки | `-` | 200 |
| `GET /api/v1/model` | Текущий выбор и установленные модели | `-` | 200 |
| `PUT /api/v1/model` | Сохранить выбор модели | `ModelChoiceWrite` | 200 |

### Добавления 0.3.0

Получение и пересчёт отбора конференции, версионное решение по докладу и четыре операции фонового выпуска описаны с полями в [контракте выпуска](release-0.3.0.md#api). Поле `screening` добавлено в элементы `/progress`, `card_only` отмечает доклад только с карточкой, `reportable` — соответствие обработки актуальному отбору (`interest.py`). Загрузка, подготовка и обработка требуют положения с извлечённым текстом. Ошибки содержат `request_id`, ответы возвращают `X-Request-ID`.

### Запросы и особые условия

Ручное изменение карточки через `PUT /talks/{id}` синхронизирует предложение подготовки и повышает его `version` в той же транзакции. `GET /conferences/{id}/preparation` для подтверждённого предложения показывает текущие поля реестра. Повторное подтверждение устаревшего черновика возвращает 409 без частичного изменения привязок; тела запросов и URL сохраняются.

Снимок OpenAPI воспроизводит декларации фабрики. `PUT /files/{file_id}/purpose` фактически возвращает пустой ответ 204, хотя декоратор объявляет 200. Это различие учтено в таблице. Для загрузки дополнительно возможен 200 при дубликате, для файлов — 206 и 416 при Range.

Загрузка материала передаёт исходные байты с `Content-Type: application/octet-stream`. Обязательный query-параметр `path` задаёт относительный путь. Дополнительные `program` и `purpose` задают роль. Повтор совпадающего содержимого возвращает 200, новая регистрация — 201. Имя файла и SHA-256 в ответе относятся к сохранённому оригиналу.

Положение передаётся тем же двоичным способом с query-параметрами `name`, `filename` и необязательным `department_id`. Непустое название отдела проверяется после удаления пробелов. Для новой версии идентификатор должен существовать.

`GET /files/{file_id}/content` отдаёт файл inline с поддержкой HTTP Range через FileResponse. Диапазон может возвращать 206, неверный диапазон — 416. Изображения страниц доступны после соответствующей подготовки. Для `/pages/{page_no}` поддерживаются номера от 1 до 300.

`POST /processing` включает флаг обработки и ставит подходящие доклады в очередь. Условия входа — наличие файлов и успешно извлечённого положения отдела. Полная обработка допускается актуальным положительным отбором. Подтверждённые связи определяют, какие доклады реально готовы. `POST /retry` возвращает 409 для уже ожидающей или выполняемой задачи.

`PUT /preparation` принимает `version` и `talks`. Каждый доклад содержит `talk_id` либо null для нового, тему, авторов, links и объяснение. Link содержит file_id и согласованную пару границ времени или страниц. Код проверяет конференцию, пересечения и пределы исходника.

Метаданные доклада получают `revision` через GET. PUT может передать её для защиты от конфликта и возвращает 204. Поля `title` и `speakers` определяют реестр справки. Связанные поля автора включают `name`, `role` и `affiliation`.

Правка сводки отправляет исходную `version`, непустой список `sentences`, список `conclusions` до трёх элементов и изменённые `claims`. Каждое предложение имеет текст и claim_ids. Индивидуальная проверка отправляет номер версии. Ответ содержит новую версию, пакет фрагментов и состояние доклада.

Массовая проверка принимает `items` до 500 элементов с talk_id и version. Успех HTTP 200 содержит `confirmed` и `skipped` с причинами. Повторы идентификаторов и доклад другой конференции отклоняются на уровне всего запроса. Недоступная версия отдельного доклада попадает в skipped.

Расчёт рекомендаций требует проверенный доклад и положения. Подтверждение отправляет `version` и список `items` с опорами. Ответ GET дополнен `job`, а готовая рекомендация имеет `reviewed` и вычисляемый `stale`.

Сборка справки принимает `talk_ids` либо null для всех, `unreviewed_confirmed` по умолчанию false. Она возвращает 201 с полным снимком ReportVersionView и предупреждениями. Скачивание использует формат `docx`, `md` или `pdf` и проверяет SHA-256 сохранённых байтов.

### Параметры путей и query

Параметры path подставляются из ответа сервера. Номер страницы задаётся целым числом. Поля пути загрузки ограничены длиной и проходят проверку безопасного относительного пути. Наименование модели в PUT /model проверяется по списку Ollama. Сброс выбора задаётся `model=null`.

Таблица ниже содержит параметры, извлечённые из OpenAPI. Поле request body рассматривается отдельно в каталоге структур.

| Операция | Параметр | Расположение | Тип | Обязателен |
| --- | --- | --- | --- | --- |
| `GET /api/v1/conferences/{conference_id}` | `conference_id` | `path` | string | да |
| `PUT /api/v1/conferences/{conference_id}` | `conference_id` | `path` | string | да |
| `DELETE /api/v1/conferences/{conference_id}` | `conference_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/materials` | `conference_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/materials` | `path` | `query` | string | да |
| `POST /api/v1/conferences/{conference_id}/materials` | `program` | `query` | boolean | нет |
| `POST /api/v1/conferences/{conference_id}/materials` | `purpose` | `query` | presentation, document, notes, program / null | нет |
| `GET /api/v1/conferences/{conference_id}/materials` | `conference_id` | `path` | string | да |
| `PUT /api/v1/conferences/{conference_id}/materials/bindings` | `conference_id` | `path` | string | да |
| `GET /api/v1/conferences/{conference_id}/conference.yaml` | `conference_id` | `path` | string | да |
| `GET /api/v1/files/{file_id}/content` | `file_id` | `path` | string | да |
| `GET /api/v1/files/{file_id}/pages/{page_no}` | `file_id` | `path` | string | да |
| `GET /api/v1/files/{file_id}/pages/{page_no}` | `page_no` | `path` | integer | да |
| `GET /api/v1/files/{file_id}/slides/{slide_no}` | `file_id` | `path` | string | да |
| `GET /api/v1/files/{file_id}/slides/{slide_no}` | `slide_no` | `path` | integer | да |
| `PUT /api/v1/files/{file_id}/purpose` | `file_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/talks` | `conference_id` | `path` | string | да |
| `PUT /api/v1/talks/{talk_id}` | `talk_id` | `path` | string | да |
| `GET /api/v1/talks/{talk_id}` | `talk_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/processing` | `conference_id` | `path` | string | да |
| `GET /api/v1/conferences/{conference_id}/progress` | `conference_id` | `path` | string | да |
| `POST /api/v1/talks/{talk_id}/retry` | `talk_id` | `path` | string | да |
| `GET /api/v1/conferences/{conference_id}/preparation` | `conference_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/preparation` | `conference_id` | `path` | string | да |
| `PUT /api/v1/conferences/{conference_id}/preparation` | `conference_id` | `path` | string | да |
| `GET /api/v1/talks/{talk_id}/summary` | `talk_id` | `path` | string | да |
| `PUT /api/v1/talks/{talk_id}/summary` | `talk_id` | `path` | string | да |
| `POST /api/v1/talks/{talk_id}/summary/review` | `talk_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/summaries/review` | `conference_id` | `path` | string | да |
| `POST /api/v1/departments` | `name` | `query` | string | да |
| `POST /api/v1/departments` | `filename` | `query` | string | да |
| `POST /api/v1/departments` | `department_id` | `query` | string / null | нет |
| `GET /api/v1/talks/{talk_id}/recommendations` | `talk_id` | `path` | string | да |
| `POST /api/v1/talks/{talk_id}/recommendations` | `talk_id` | `path` | string | да |
| `PUT /api/v1/talks/{talk_id}/recommendations` | `talk_id` | `path` | string | да |
| `GET /api/v1/conferences/{conference_id}/reports` | `conference_id` | `path` | string | да |
| `POST /api/v1/conferences/{conference_id}/reports` | `conference_id` | `path` | string | да |
| `GET /api/v1/report-versions/{version_id}/files/{report_format}` | `version_id` | `path` | string | да |
| `GET /api/v1/report-versions/{version_id}/files/{report_format}` | `report_format` | `path` | ReportFormat | да |

### Внутренний ASR

Сервис ASR слушает 18764 внутри сети проекта. `GET /health` проверяет наличие `model.bin`, возвращая 503 при его отсутствии. `POST /transcribe` получает JSON `{"sha256":"<64 hex>"}`. SHA-256 должен соответствовать локальному blob в `/data`.

Ответ содержит `segments` с start_ms, end_ms, text и поле revision. Отсутствующий blob даёт 404. Проверки длительности, чтение и ошибки ffmpeg/ffprobe возвращают 422. Структура ошибки FastAPI этого сервиса содержит `detail`. App преобразует сбой ASR в свою ошибку этапа.

### Каталог структур

Ниже приведены поля всех 72 структур схемы приложения. Обязательность относится к JSON-контракту. Символ `/ null` обозначает допустимое значение null, которое само по себе не делает поле необязательным. Условия связей дополнительно проверяются предметными валидаторами и описаны выше.

### `ActiveModel`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `profile` | `string` | да | - |
| `model` | `string` | да | - |
| `reasoning` | `boolean` | да | - |
| `chosen` | `boolean` | да | - |

### `Binding`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talk_id` | `string / null` | да | - |
| `confirmed` | `boolean` | да | - |
| `confidence` | `number / null` | да | - |

### `BindingChoice`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `source_file_id` | `string` | да | - |
| `talk_id` | `string / null` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `BindingReview`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `bindings` | `массив BindingChoice` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `BuildRequest`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `unreviewed_confirmed` | `boolean` | нет | default=false |
| `talk_ids` | `массив string / null` | нет | - |
| `idempotency_key` | `string` | да | minLength=1, maxLength=100 |

Дополнительные поля запрещены контрактом этой структуры.

### `BulkItem`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talk_id` | `string` | да | - |
| `version` | `integer` | да | - |

### `BulkReview`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `items` | `массив BulkItem` | да | maxItems=500 |

### `CheckName`

Допустимые значения или тип — `claim_links, sentence_count, numbers_in_sources, forbidden_phrases, critic, reader_context`.

### `CheckResult`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `check` | `CheckName` | да | - |
| `passed` | `boolean` | да | - |
| `attempt` | `integer` | да | - |
| `details` | `string / null` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `Claim`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | format="uuid" |
| `key` | `string` | да | pattern="^c&#91;1-9&#93;&#91;0-9&#93;*$" |
| `type` | `ClaimType` | да | - |
| `text` | `string` | да | minLength=1 |
| `status` | `ClaimStatus` | да | - |
| `evidence` | `массив Evidence` | да | - |
| `rejected_evidence` | `массив RejectedEvidence` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ClaimEdit`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `key` | `string` | да | pattern="^c&#91;1-9&#93;&#91;0-9&#93;*$" |
| `text` | `string` | да | minLength=1 |

Дополнительные поля запрещены контрактом этой структуры.

### `ClaimStatus`

Допустимые значения или тип — `rejected, verified, needs_human, confirmed`.

### `ClaimType`

Допустимые значения или тип — `problem, method, result, conclusion, listener_remark`.

### `ConferenceItem`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | - |
| `title` | `string / null` | да | - |
| `start_date` | `string / null` | да | - |
| `end_date` | `string / null` | да | - |
| `place` | `string / null` | да | - |
| `organizer` | `string / null` | да | - |
| `processing_started` | `boolean` | да | - |
| `talks` | `integer` | да | - |
| `summaries` | `integer` | да | - |
| `reviewed` | `integer` | да | - |

### `ConferenceWrite`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `title` | `string / null` | нет | - |
| `start_date` | `string / null` | нет | - |
| `end_date` | `string / null` | нет | - |
| `place` | `string / null` | нет | - |
| `organizer` | `string / null` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `Evidence`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `label` | `string` | да | pattern="^(?:&#91;SVD&#93;(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;1-9&#93;&#91;0-9&#93;*\|T(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;0-9&#93;{2}:&#91;0-9&#93;{2}:&#91;0-9&#93;{2}\|N&#91;1-9&#93;&#91;0-9&#93;*)$" |
| `quote` | `string` | да | minLength=1 |
| `fragment_revision_id` | `string` | да | format="uuid" |
| `span` | `массив object` | да | minItems=2, maxItems=2 |
| `match_score` | `number` | да | minimum=0.0, maximum=1.0 |

Дополнительные поля запрещены контрактом этой структуры.

### `EvidenceDraft`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `label` | `string` | да | pattern="^(?:&#91;SVD&#93;(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;1-9&#93;&#91;0-9&#93;*\|T(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;0-9&#93;{2}:&#91;0-9&#93;{2}:&#91;0-9&#93;{2}\|N&#91;1-9&#93;&#91;0-9&#93;*)$" |
| `quote` | `string` | да | minLength=1 |

Дополнительные поля запрещены контрактом этой структуры.

### `EvidenceRejection`

Допустимые значения или тип — `label_not_found, quote_not_found, below_threshold, number_mismatch, negation_mismatch, too_short`.

### `FactDraft`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `text` | `string` | да | minLength=1 |
| `evidence` | `массив EvidenceDraft` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `FactsDraft`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `title` | `string / null` | да | - |
| `speakers` | `массив string` | да | - |
| `affiliations` | `массив string` | да | - |
| `talk_type` | `TalkType` | да | - |
| `problem` | `FactDraft / null` | да | - |
| `methods` | `массив FactDraft` | да | - |
| `results` | `массив FactDraft` | да | - |
| `conclusions` | `массив FactDraft` | да | - |
| `conclusions_absent_reason` | `string / null` | да | - |
| `listener_remarks` | `массив FactDraft` | да | - |
| `uncertain` | `массив string` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `FileKind`

Допустимые значения или тип — `presentation, audio, video, notes, program, document, image`.

### `FragmentKind`

Допустимые значения или тип — `slide, transcript, note, program, slide_description, document`.

### `HTTPValidationError`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `detail` | `массив ValidationError` | нет | - |

### `LinesLocator`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `kind` | `string` | да | - |
| `line_start` | `integer` | да | - |
| `line_end` | `integer` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `Link`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `file_id` | `string` | да | - |
| `start_ms` | `integer / null` | нет | - |
| `end_ms` | `integer / null` | нет | - |
| `page_start` | `integer / null` | нет | - |
| `page_end` | `integer / null` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `Material`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `source` | `SourceFile` | да | - |
| `relative_path` | `string` | да | - |
| `title_candidates` | `TitleCandidates / null` | да | - |
| `warning` | `string / null` | да | - |
| `binding` | `Binding` | да | - |
| `suggested_talk_id` | `string / null` | нет | - |
| `confidence` | `number / null` | нет | - |
| `alternate_of` | `string / null` | нет | - |
| `range_talk_ids` | `массив string` | нет | default=[] |

### `Materials`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `processing_started` | `boolean` | да | - |
| `files` | `массив Material` | да | - |
| `sections` | `массив ReviewSection` | да | - |
| `talks` | `массив ReviewTalk` | да | - |

### `ModelChoiceWrite`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `model` | `string / null` | да | - |
| `reasoning` | `boolean` | нет | default=false |

Дополнительные поля запрещены контрактом этой структуры.

### `ModelState`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `current` | `ActiveModel` | да | - |
| `default` | `ActiveModel` | да | - |
| `ollama_available` | `boolean` | да | - |
| `models` | `массив OllamaModel` | да | - |

### `OllamaModel`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `name` | `string` | да | - |
| `parameter_size` | `string / null` | да | - |
| `quantization` | `string / null` | да | - |
| `size_bytes` | `integer` | да | - |
| `digest` | `string` | да | - |
| `thinking` | `boolean` | да | - |

### `PackBlock`

Допустимые значения или тип — `main, qa`.

### `PackedFragment`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `label` | `string` | да | pattern="^(?:&#91;SVD&#93;(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;1-9&#93;&#91;0-9&#93;*\|T(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;0-9&#93;{2}:&#91;0-9&#93;{2}:&#91;0-9&#93;{2}\|N&#91;1-9&#93;&#91;0-9&#93;*)$" |
| `kind` | `FragmentKind` | да | - |
| `fragment_id` | `string` | да | format="uuid" |
| `revision_id` | `string` | да | format="uuid" |
| `source_file_id` | `string` | да | format="uuid" |
| `locator` | `object` | да | - |
| `origin` | `TextOrigin` | да | - |
| `text` | `string` | да | - |
| `block` | `PackBlock` | да | - |
| `low_quality` | `boolean` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `PageLocator`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `kind` | `string` | да | - |
| `page_no` | `integer` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `PersonRole`

Допустимые значения или тип — `speaker, coauthor`.

### `ProfileInfo`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `name` | `string` | да | - |
| `model` | `string` | да | - |
| `reasoning` | `boolean` | да | - |
| `prompt_version` | `string` | да | - |
| `default` | `boolean` | да | - |
| `available` | `boolean / null` | нет | модель есть в каталоге моделей; null — каталог не подключён |
| `used` | `boolean` | нет | default=false; профиль, по которому работает приложение |

### `Progress`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `processing_started` | `boolean` | да | - |
| `files` | `integer` | да | - |
| `talks` | `массив TalkProgress` | да | - |

### `ProposalWrite`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `integer` | да | minimum=1.0 |
| `talks` | `массив ProposedTalk` | да | maxItems=500 |

### `ProposedTalk`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talk_id` | `string / null` | нет | - |
| `title` | `string` | да | minLength=1, maxLength=1000 |
| `speakers` | `массив Speaker` | нет | - |
| `links` | `массив Link` | да | minItems=1 |
| `reason` | `string` | нет | default="" |

### `PurposeWrite`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `purpose` | `presentation, document, notes, program` | да | - |

### `Recommendation`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `department_id` | `string` | да | - |
| `department_version` | `integer` | да | minimum=1.0 |
| `reason` | `string` | да | minLength=1, maxLength=3000 |
| `claim_ids` | `массив string` | да | minItems=1 |
| `paragraph_ids` | `массив string` | да | minItems=1 |

### `RejectedEvidence`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `label` | `string` | да | pattern="^(?:&#91;SVD&#93;(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;1-9&#93;&#91;0-9&#93;*\|T(?:&#91;1-9&#93;&#91;0-9&#93;*\\.)?&#91;0-9&#93;{2}:&#91;0-9&#93;{2}:&#91;0-9&#93;{2}\|N&#91;1-9&#93;&#91;0-9&#93;*)$" |
| `quote` | `string` | да | minLength=1 |
| `reason` | `EvidenceRejection` | да | - |
| `best_score` | `number / null` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportBuild`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `unreviewed_confirmed` | `boolean` | нет | default=false |
| `talk_ids` | `массив string / null` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportFile`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `format` | `ReportFormat` | да | - |
| `storage_key` | `string` | да | minLength=1 |
| `sha256` | `string` | да | pattern="^&#91;0-9a-f&#93;{64}$" |
| `size_bytes` | `integer` | да | minimum=0.0 |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportFormat`

Допустимые значения или тип — `md, docx, pdf`.

### `ReportInput`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `conference_title` | `string / null` | да | - |
| `period_start` | `string / null` | да | - |
| `period_end` | `string / null` | да | - |
| `place` | `string / null` | да | - |
| `organizer` | `string / null` | да | - |
| `selection` | `ReportSelection` | да | - |
| `talks` | `массив ReportTalk` | да | - |
| `skipped` | `массив SkippedTalk` | да | - |
| `template_version` | `string` | да | minLength=1 |
| `unreviewed_confirmed` | `boolean` | да | - |
| `department_recommendations` | `массив object` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportSelection`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `days` | `массив string` | да | - |
| `section_ids` | `массив string` | да | - |
| `talk_ids` | `массив string` | да | minItems=1 |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportTalk`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talk_id` | `string` | да | format="uuid" |
| `section_title` | `string` | да | minLength=1 |
| `title` | `string` | да | minLength=1 |
| `speakers` | `массив string` | да | - |
| `affiliations` | `массив string` | да | - |
| `status` | `TalkStatus` | да | - |
| `summary` | `string` | да | minLength=1 |
| `conclusions` | `массив string` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportVariables`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talks_count` | `integer` | да | minimum=0.0 |
| `speakers_count` | `integer` | да | minimum=0.0 |

Дополнительные поля запрещены контрактом этой структуры.

### `ReportVersionView`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | format="uuid" |
| `conference_id` | `string` | да | format="uuid" |
| `number` | `integer` | да | - |
| `input` | `ReportInput` | да | - |
| `variables` | `ReportVariables` | да | - |
| `files` | `массив ReportFile` | да | - |
| `created_at` | `string` | да | format="date-time" |
| `warnings` | `массив string` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ReviewSection`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | - |
| `title` | `string` | да | - |

### `ReviewTalk`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | - |
| `section_id` | `string` | да | - |
| `section` | `string` | да | - |
| `order` | `integer` | да | - |
| `title` | `string / null` | да | - |
| `status` | `TalkStatus` | да | - |
| `speakers` | `массив Speaker` | нет | default=[] |

### `SkippedTalk`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `talk_id` | `string` | да | format="uuid" |
| `title` | `string / null` | да | - |
| `status` | `TalkStatus` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `SourceFile`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | format="uuid" |
| `conference_id` | `string` | да | format="uuid" |
| `sha256` | `string` | да | pattern="^&#91;0-9a-f&#93;{64}$" |
| `original_name` | `string` | да | minLength=1 |
| `kind` | `FileKind` | да | - |
| `media_type` | `string` | да | minLength=1 |
| `size_bytes` | `integer` | да | minimum=0.0 |
| `duration_ms` | `integer / null` | да | - |
| `encoding` | `string / null` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `Speaker`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `name` | `string` | да | minLength=1 |
| `affiliation` | `string / null` | нет | - |
| `role` | `PersonRole` | нет | default="speaker" |

Дополнительные поля запрещены контрактом этой структуры.

### `Summary`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | format="uuid" |
| `talk_id` | `string` | да | format="uuid" |
| `version` | `integer` | да | - |
| `status` | `SummaryStatus` | да | - |
| `profile` | `string` | да | minLength=1 |
| `prompt_versions` | `object` | да | - |
| `input_revision_ids` | `массив string` | да | - |
| `facts` | `FactsDraft` | да | - |
| `claims` | `массив Claim` | да | - |
| `sentences` | `массив SummaryLine` | да | - |
| `conclusions` | `массив SummaryLine` | да | - |
| `checks` | `массив CheckResult` | да | - |
| `human_edit` | `boolean` | да | - |
| `created_at` | `string` | да | format="date-time" |

Дополнительные поля запрещены контрактом этой структуры.

### `SummaryEdit`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `integer` | да | - |
| `sentences` | `массив SummaryLine` | да | minItems=1 |
| `conclusions` | `массив SummaryLine` | да | maxItems=3 |
| `claims` | `массив ClaimEdit` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `SummaryLine`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `text` | `string` | да | minLength=1 |
| `claim_ids` | `массив string` | да | minItems=1 |

Дополнительные поля запрещены контрактом этой структуры.

### `SummaryReview`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `integer` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `SummaryStatus`

Допустимые значения или тип — `ready, needs_attention, stale`.

### `SystemInfo`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `string` | да | - |
| `ollama_url` | `string` | да | - |
| `ollama_available` | `boolean` | да | - |
| `ollama_models` | `массив string` | да | - |
| `profiles` | `массив ProfileInfo` | да | - |
| `profile_note` | `string / null` | нет | почему используется не заданный профиль |
| `templates` | `массив TemplateInfo` | да | - |
| `queue` | `object` | да | - |

### `TalkCreate`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `title` | `string` | да | minLength=1 |
| `section_id` | `string / null` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `TalkFragmentPack`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `conference_id` | `string` | да | format="uuid" |
| `talk_id` | `string` | да | format="uuid" |
| `fragments` | `массив PackedFragment` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `TalkProgress`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `id` | `string` | да | - |
| `section_id` | `string` | да | - |
| `section` | `string` | да | - |
| `order` | `integer` | да | - |
| `title` | `string / null` | да | - |
| `status` | `TalkStatus` | да | - |
| `job` | `queued, running, done, failed / null` | да | - |
| `step` | `string / null` | да | - |
| `error` | `string / null` | да | - |
| `summary_number` | `integer / null` | да | - |
| `summary_status` | `SummaryStatus / null` | да | - |
| `screening` | `object / null` | нет | - |
| `bulk_reviewable` | `boolean` | нет | default=false |
| `card_only` | `boolean` | нет | default=false |
| `reportable` | `boolean` | нет | default=false |

### `TalkStatus`

Допустимые значения или тип — `draft, matched, processed, reviewed, stale, not_held, insufficient_data`.

### `TalkSummaryView`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `summary` | `Summary` | да | - |
| `pack` | `TalkFragmentPack` | да | - |
| `talk_title` | `string / null` | да | - |
| `talk_status` | `TalkStatus` | да | - |
| `speakers` | `массив Speaker` | нет | - |
| `conference_id` | `string` | нет | default="" |
| `job` | `string / null` | нет | - |

### `TalkType`

Допустимые значения или тип — `исследовательский, обзорный, прикладной, иное`.

### `TalkUpdate`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `title` | `string / null` | да | - |
| `speakers` | `массив Speaker` | нет | - |
| `revision` | `integer / null` | нет | - |

Дополнительные поля запрещены контрактом этой структуры.

### `TemplateInfo`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `name` | `string` | да | - |
| `version` | `string` | да | - |

### `TextOrigin`

Допустимые значения или тип — `extracted, ocr, asr, vlm, term_fix, human`.

### `TimeLocator`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `kind` | `string` | да | - |
| `start_ms` | `integer` | да | minimum=0.0 |
| `end_ms` | `integer` | да | minimum=0.0 |

Дополнительные поля запрещены контрактом этой структуры.

### `TitleCandidates`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `title` | `string / null` | да | - |
| `speakers` | `массив string` | да | - |
| `affiliations` | `массив string` | да | - |

Дополнительные поля запрещены контрактом этой структуры.

### `ValidationError`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `loc` | `массив string / integer` | да | - |
| `msg` | `string` | да | - |
| `type` | `string` | да | - |
| `input` | `object` | нет | - |
| `ctx` | `object` | нет | - |

### `rapporteur__departments__Review`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `integer` | да | minimum=1.0 |
| `items` | `массив Recommendation` | да | - |

### `rapporteur__screening__Review`

| Поле | Тип | Обязательно | Ограничения и default |
| --- | --- | --- | --- |
| `version` | `integer` | да | minimum=1.0 |
| `department_ids` | `массив string` | да | maxItems=1000 |

Дополнительные поля запрещены контрактом этой структуры.

## Доступ к API

API предоставляет единую рабочую область. Учётные записи и токены доступа текущая фабрика приложения не настраивает. Сетевой доступ контролируется инфраструктурой организации. При публикации адреса администратор определяет разрешённых клиентов.

Служебные пути ASR и Ollama находятся в Docker-сети. ASR принимает только content-addressed blob из общего хранилища. Загрузка стороннего URL в интерфейс этого сервиса не входит.

## Ошибки

Ошибка доступа к рабочему хранилищу во время HTTP-операции возвращает 503 и `code=storage_unwritable`. В сообщении есть путь, числовые UID/GID и команда восстановления; сохраняется request ID. Отказы доступа вне рабочего тома не выдаются за эту неисправность. [Проверка и восстановление](storage-permissions.md).

App возвращает предметные ошибки как `{"message":"текст","request_id":"id"}`. Дополнительное поле `code` обозначает машинный код предметного отказа. `X-Request-ID` позволяет сопоставить ответ с журналом, включая неожиданные ошибки 500. Ошибки входной Pydantic-валидации также приводятся к этому виду с HTTP 422. На уровне двоичных ответов и инфраструктуры возможны стандартные ответы ASGI-сервера.

| Код | Значение в приложении |
| --- | --- |
| 404 | Объект, подготовленная страница или версия отсутствует |
| 409 | Прочитанная версия изменилась, задача уже работает, путь занят другим содержимым |
| 413 | Превышен допустимый размер |
| 415 | Неверный Content-Type загрузки |
| 422 | Неверная структура, диапазон, формат или условие предметного действия |
| 503 | Ollama недоступна при сохранении нового выбора модели |
| 500 | Внутренняя ошибка либо нарушение целостности файла справки |

Прогресс задач хранит `error` отдельно от HTTP-ошибок. StageError использует коды unsupported_format, broken_file, encrypted_file, file_too_large, too_many_pages, archive_limits, render_failed, no_sources, context_overflow, llm_unavailable, llm_truncated, llm_invalid_output и report_invalid. Подготовка и рекомендации также сохраняют текст ошибки задачи.

В схемах OpenAPI перечислены объявленные responses. Предметные коды 404/409/503 дополнительно формируются обработчиками и описаны в этом разделе. При конфликте повторно прочитайте объект, сопоставьте изменения и отправьте новую осознанную правку.

## Примеры

Сначала создайте конференцию. Команды предполагают стандартный локальный адрес.

```bash
curl --fail-with-body -X POST http://127.0.0.1:18763/api/v1/conferences \
  -H 'Content-Type: application/json' \
  --data '{"title":"Учебная конференция","start_date":"2026-11-15","end_date":"2026-11-15"}'
```

Скопируйте id ответа в переменную и загрузите текст выступления.

```bash
CONFERENCE_ID=идентификатор-из-ответа
curl --fail-with-body -X POST \
  "http://127.0.0.1:18763/api/v1/conferences/$CONFERENCE_ID/materials?path=speech.txt&purpose=document" \
  -H 'Content-Type: application/octet-stream' --data-binary @speech.txt
curl --fail-with-body -X POST \
  "http://127.0.0.1:18763/api/v1/conferences/$CONFERENCE_ID/preparation"
curl --fail-with-body \
  "http://127.0.0.1:18763/api/v1/conferences/$CONFERENCE_ID/preparation"
```

Дождитесь job=done. Сохраните прочитанные version и talks, проверьте их и отправьте PUT на тот же путь. Затем отправьте POST на processing. Эту последовательность полностью выполняет интерфейс. Для сводки используйте talk_id из progress, для подтверждения отправьте её текущую version.

Пример тела подтверждения рекомендации показывает форму полей. Идентификаторы должны быть взяты из текущего ответа сервера.

```json
{"version":1,"items":[{"department_id":"идентификатор-отдела","department_version":1,"reason":"Отдел исследует политики кэширования","claim_ids":["c3"],"paragraph_ids":["p1.1"]}]}
```

Схемы доступны отдельными файлами [API приложения](api-openapi.json) и [ASR](asr-openapi.json). Число операций и полей сверяется с этими снимками при проверке документации.

## Связанные документы

- [Модель данных](data-model.md)
- [Правила сводки](facts.md)
- [Пользовательский путь](workflow.md)

## Источники

- Группы маршрутов. [src/rapporteur/api/__init__.py](../src/rapporteur/api/__init__.py)
- Конференции. [src/rapporteur/api/conferences.py](../src/rapporteur/api/conferences.py)
- Файлы. [src/rapporteur/api/files.py](../src/rapporteur/api/files.py)
- Доклады. [src/rapporteur/api/talks.py](../src/rapporteur/api/talks.py)
- Очереди и подготовка. [src/rapporteur/api/workflow.py](../src/rapporteur/api/workflow.py)
- Сводки. [src/rapporteur/api/summaries.py](../src/rapporteur/api/summaries.py)
- Отделы. [src/rapporteur/api/departments.py](../src/rapporteur/api/departments.py)
- Справки. [src/rapporteur/api/reports.py](../src/rapporteur/api/reports.py)
- Настройки. [src/rapporteur/api/system.py](../src/rapporteur/api/system.py)
- Ошибки и служебные пути. [src/rapporteur/app.py](../src/rapporteur/app.py)
- ASR. [src/rapporteur/asr_service.py](../src/rapporteur/asr_service.py)
- Вложенные контракты. [src/rapporteur/contracts](../src/rapporteur/contracts)
