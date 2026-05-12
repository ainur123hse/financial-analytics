# Сервис `app`: техническое описание, схемы данных и потоков обработки

## 1. Назначение сервиса

Сервис `app` представляет собой прикладной слой системы генерации финансовой аналитики по корпоративным документам. Его задача состоит в том, чтобы:

1. аутентифицировать пользователя;
2. изолировать рабочие данные по приватным тредам;
3. принимать исходные документы двух типов;
4. преобразовывать документы в Markdown-представление, пригодное для последующей машинной обработки;
5. запускать асинхронную генерацию новой аналитической записки по заданному целевому периоду;
6. сохранять историю пользовательских запросов и сгенерированных ответов.

С точки зрения предметной области сервис реализует следующий типовой сценарий:

1. Пользователь регистрируется или входит в систему.
2. Пользователь создаёт тред, который становится контейнером для данных одной аналитической задачи.
3. В тред загружается аналитика прошлых периодов с `document_kind=analytics`.
4. В тот же тред загружаются релевантные первичные источники с `document_kind=sources`.
5. Фоновый воркер преобразует документы в Markdown и подготавливает рабочее файловое пространство треда.
6. Пользователь указывает свободно сформулированное описание целевого периода.
7. Фоновый воркер запускает отдельный `Codex`-runner, который на основе накопленного Markdown-корпуса создаёт новую аналитическую записку в формате Markdown.
8. Результат сохраняется в историю сообщений треда и возвращается через API статуса задачи.

Описание ниже подготовлено по текущей реализации репозитория и относится прежде всего к каталогам `app/`, `alembic/`, `tests/`, а также к файлам `Dockerfile`, `docker-compose.yml` и `docker-compose.langfuse.yml`.

## 2. Архитектурный обзор

### 2.1. Компонентный контур

```mermaid
flowchart TD
    A[Браузер] --> B[FastAPI application]
    B --> C[(PostgreSQL)]
    B --> D[(Redis)]
    B --> E[Celery broker enqueue]
    E --> F[Celery worker]
    F --> C
    F --> D
    F --> G[Docling preprocessing]
    G --> H[(Markdown volume)]
    F --> I[/var/run/docker.sock]
    I --> J[One-off Docker containers]
    J --> K[Codex runner]
    G --> L[LLM via OpenRouter/API]
    K --> L
    F --> M[Langfuse tracing]
```

Архитектура разделена на несколько изолированных слоёв:

- HTTP-слой реализован на `FastAPI`.
- Персистентная бизнес-модель хранится в `PostgreSQL` через `SQLAlchemy`.
- Координация конкурентной загрузки документов производится через `Redis`.
- Асинхронные операции выполняются `Celery`-воркером.
- Промежуточное файловое представление документов хранится в локальном каталоге или Docker volume `markdowns`.
- Генерация новой аналитики выполняется не в основном процессе API, а в отдельном Docker-контейнере с установленным `Codex`.
- Трассировка генерации опционально отправляется в `Langfuse`.

### 2.2. Основные подсистемы и их роли

| Подсистема | Реализация | Назначение |
| --- | --- | --- |
| Web/API | `app/main.py`, `app/api/routes.py` | HTTP-вход в систему, маршруты, статический frontend |
| Схемы API | `app/api/schemas.py` | Контракты запросов и ответов |
| Аутентификация | `app/auth.py`, `app/api/dependencies.py` | Cookie-сессия на базе JWT, идентификация пользователя |
| База данных | `app/db.py`, `app/models.py`, `alembic/` | ORM-модель, соединения, миграции |
| Файловое хранилище | `app/storage.py` | Структура каталогов тредов и документов |
| Координация параллелизма | `app/api/redis_store.py` | Блокировка конфликтующих `stem` в рамках треда |
| Очередь задач | `app/api/celery_app.py`, `app/api/tasks.py` | Асинхронная конвертация и генерация |
| Preprocessing документов | `app/documents_preprocessing/*` | Конвертация PDF/HTML в Markdown, OCR, работа с изображениями |
| Генерация аналитики | `app/codex_runner/*` | Подготовка snapshot workspace и запуск `Codex` в Docker |
| LLM-интеграция | `app/llm_client.py`, `app/llm_utils.py` | Вызовы моделей для анализа изображений |
| Наблюдаемость | `app/langfuse_client.py` | Трассировка генерации и шагов runner |
| Browser UI | `app/web/index.html`, `app/web/app.js`, `app/web/styles.css` | Встроенный одностраничный интерфейс |

### 2.3. Основной пользовательский поток

```mermaid
sequenceDiagram
    participant U as User
    participant W as Web UI
    participant API as FastAPI
    participant DB as PostgreSQL
    participant R as Redis
    participant Q as Celery
    participant WRK as Worker
    participant FS as Markdown storage
    participant DKR as Docker/Codex

    U->>W: Вход / регистрация
    W->>API: Auth request
    API->>DB: User lookup/create
    API-->>W: Cookie session

    U->>W: Создать тред
    W->>API: POST /threads
    API->>DB: Insert thread
    API->>FS: Create thread directories

    U->>W: Загрузить документы
    W->>API: POST /conversions
    API->>DB: Create conversion job
    API->>R: Reserve stems
    API->>Q: Enqueue task
    Q->>WRK: convert_document_batch
    WRK->>FS: Write markdown/images
    WRK->>DB: Insert documents, complete job

    U->>W: Задать целевой период
    W->>API: POST /generations
    API->>DB: Insert user message + generation job
    API->>Q: Enqueue task
    Q->>WRK: generate_analytics_by_period
    WRK->>FS: Snapshot thread workspace
    WRK->>DKR: Run Codex container
    DKR->>FS: Write generated markdown
    WRK->>DB: Insert assistant message, complete job
    API-->>W: Polling status result
```

## 3. Структура кода сервиса `app`

### 3.1. Точка входа

Файл `app/main.py` создаёт `FastAPI(title="Financial Analytics API")`, подключает API-router и публикует встроенный фронтенд:

- `app.include_router(api_router)` подключает все маршруты `prefix="/api/v1"`;
- `WEB_DIR = app/web`;
- `app.mount("/static", StaticFiles(...), name="static")` делает доступными `styles.css` и `app.js`;
- `GET /` возвращает `index.html`.

Таким образом, `app` содержит не только backend API, но и минимальный браузерный интерфейс, обслуживаемый тем же приложением.

### 3.2. Модульное разбиение

| Путь | Содержательная роль |
| --- | --- |
| `app/api/routes.py` | Основная бизнес-логика HTTP-маршрутов |
| `app/api/schemas.py` | Pydantic-контракты API |
| `app/api/dependencies.py` | Зависимость `get_current_user` |
| `app/api/redis_store.py` | Lua-скрипты резервирования и освобождения stem-локов |
| `app/api/celery_app.py` | Инициализация Celery с JSON-сериализацией |
| `app/api/tasks.py` | Реализация двух фоновых задач |
| `app/auth.py` | Хеширование паролей, выпуск и декодирование JWT-сессий |
| `app/db.py` | `engine`, `SessionLocal`, `session_scope()` |
| `app/models.py` | ORM-сущности и доменные константы |
| `app/storage.py` | Имена каталогов и файловые операции |
| `app/documents_preprocessing/make_markdown.py` | Полный pipeline преобразования документа в Markdown |
| `app/documents_preprocessing/docling_converter.py` | Адаптер к `Docling`, OCR backend selection |
| `app/documents_preprocessing/schema.py` | Схемы структурированных ответов LLM по изображениям |
| `app/documents_preprocessing/prompt.py` | Промпты для классификации графиков и описания изображений |
| `app/codex_runner/prompt.py` | Системное задание на генерацию новой аналитики |
| `app/codex_runner/run.py` | Подготовка Docker workspace, запуск `Codex`, разбор логов |
| `app/codex_runner/config.py` | Пути, шаблоны контейнеров и regex-константы |
| `app/codex_runner/config.toml` | Конфигурация клиента `codex` внутри контейнера |
| `app/llm_client.py` | Асинхронный клиент OpenAI-compatible API |
| `app/llm_utils.py` | Сборка multimodal message и чтение ответов |
| `app/langfuse_client.py` | Безопасная интеграция с `Langfuse` |
| `app/web/app.js` | SPA-логика, polling задач и рендеринг тредов |

## 4. Предметная модель и модель данных

### 4.1. Доменные константы

| Константа | Значение | Смысл |
| --- | --- | --- |
| `DEFAULT_THREAD_TITLE` | `Новый тред` | Заголовок треда по умолчанию |
| `DOCUMENT_KIND_ANALYTICS` | `analytics` | Аналитика прошлых периодов |
| `DOCUMENT_KIND_SOURCES` | `sources` | Релевантные исходные документы |
| `JOB_KIND_CONVERSION` | `conversion` | Задача конвертации документов |
| `JOB_KIND_GENERATION` | `generation` | Задача генерации аналитики |
| `JOB_STATUS_QUEUED` | `queued` | Задача поставлена в очередь |
| `JOB_STATUS_RUNNING` | `running` | Задача выполняется |
| `JOB_STATUS_COMPLETED` | `completed` | Задача завершилась успешно |
| `JOB_STATUS_FAILED` | `failed` | Задача завершилась ошибкой |
| `ACTIVE_JOB_STATUSES` | `{"queued", "running"}` | Статусы, считающиеся активными |
| `ANALYTICS_DIRNAME` | `аналитика` | Каталог converted analytics в workspace |
| `DOCUMENTS_DIRNAME` | `документы` | Каталог converted sources в workspace |

### 4.2. ER-диаграмма

```mermaid
erDiagram
    USERS ||--o{ THREADS : owns
    USERS ||--o{ JOBS : creates
    THREADS ||--o{ DOCUMENTS : contains
    THREADS ||--o{ MESSAGES : contains
    THREADS ||--o{ JOBS : owns

    USERS {
        string id PK
        string email UK
        string password_hash
        datetime created_at
    }

    THREADS {
        string id PK
        string user_id FK
        string title
        datetime created_at
        datetime updated_at
    }

    DOCUMENTS {
        string id PK
        string thread_id FK
        string kind
        string original_filename
        string stem
        string markdown_filename
        string images_dirname
        string artifacts_dirname
        datetime created_at
    }

    MESSAGES {
        string id PK
        string thread_id FK
        string role
        text content
        datetime created_at
    }

    JOBS {
        string id PK
        string user_id FK
        string thread_id FK
        string kind
        string status
        text error
        json result_payload
        datetime created_at
        datetime completed_at
    }
```

### 4.3. Таблица `users`

| Поле | Тип | Null | Назначение |
| --- | --- | --- | --- |
| `id` | `String(36)` | Нет | UUID пользователя |
| `email` | `String(320)` | Нет | Нормализованный email |
| `password_hash` | `String(255)` | Нет | Хеш пароля |
| `created_at` | `DateTime(timezone=True)` | Нет | Время создания |

Ограничения и индексы:

- Primary key: `id`.
- Unique index: `ix_users_email`.

### 4.4. Таблица `threads`

| Поле | Тип | Null | Назначение |
| --- | --- | --- | --- |
| `id` | `String(36)` | Нет | UUID треда |
| `user_id` | `String(36)` | Нет | Владелец треда |
| `title` | `String(255)` | Нет | Человекочитаемое имя треда |
| `created_at` | `DateTime(timezone=True)` | Нет | Время создания |
| `updated_at` | `DateTime(timezone=True)` | Нет | Время последнего обновления |

Ограничения и индексы:

- Primary key: `id`.
- Foreign key: `user_id -> users.id ON DELETE CASCADE`.
- Index: `ix_threads_user_id`.

Поведение:

- удаление пользователя каскадно удаляет его треды;
- `updated_at` обновляется при изменении названия треда, при добавлении/удалении документов и при добавлении сообщений генерации;
- выдача списка тредов сортируется по `updated_at DESC, created_at DESC`.

### 4.5. Таблица `documents`

| Поле | Тип | Null | Назначение |
| --- | --- | --- | --- |
| `id` | `String(36)` | Нет | UUID документа |
| `thread_id` | `String(36)` | Нет | Идентификатор треда |
| `kind` | `String(32)` | Нет | `analytics` или `sources` |
| `original_filename` | `String(255)` | Нет | Имя загруженного файла |
| `stem` | `String(255)` | Нет | Нормализованное имя без расширения |
| `markdown_filename` | `String(255)` | Нет | Имя итогового `.md` |
| `images_dirname` | `String(255)` | Нет | Каталог изображений документа |
| `artifacts_dirname` | `String(255)` | Нет | Технический каталог артефактов |
| `created_at` | `DateTime(timezone=True)` | Нет | Время регистрации результата |

Ограничения и индексы:

- Primary key: `id`.
- Foreign key: `thread_id -> threads.id ON DELETE CASCADE`.
- Index: `ix_documents_thread_id`.
- Index: `ix_documents_kind`.
- Unique constraint: `uq_documents_thread_kind_stem` на `(thread_id, kind, stem)`.

Ключевая семантика:

- одинаковый `stem` допускается в одном треде только если документы относятся к разным `kind`;
- `artifacts_dirname` хранится в БД даже при том, что успешный preprocessing обычно удаляет соответствующий каталог после завершения конвертации.

### 4.6. Таблица `messages`

| Поле | Тип | Null | Назначение |
| --- | --- | --- | --- |
| `id` | `String(36)` | Нет | UUID сообщения |
| `thread_id` | `String(36)` | Нет | Тред сообщения |
| `role` | `String(32)` | Нет | Роль отправителя, фактически `user` или `assistant` |
| `content` | `Text` | Нет | Текст периода или Markdown-ответ |
| `created_at` | `DateTime(timezone=True)` | Нет | Время добавления |

Ограничения и индексы:

- Primary key: `id`.
- Foreign key: `thread_id -> threads.id ON DELETE CASCADE`.
- Index: `ix_messages_thread_id`.

Поведение:

- выдача истории сообщений упорядочена по `created_at ASC, id ASC`;
- пользовательский prompt для генерации сохраняется как сообщение `role="user"`;
- итоговая аналитика сохраняется как сообщение `role="assistant"`.

### 4.7. Таблица `jobs`

| Поле | Тип | Null | Назначение |
| --- | --- | --- | --- |
| `id` | `String(36)` | Нет | UUID задачи |
| `user_id` | `String(36)` | Нет | Пользователь-владелец |
| `thread_id` | `String(36)` | Нет | Тред, к которому относится задача |
| `kind` | `String(32)` | Нет | `conversion` или `generation` |
| `status` | `String(32)` | Нет | `queued`, `running`, `completed`, `failed` |
| `error` | `Text` | Да | Текст ошибки |
| `result_payload` | `JSON` | Да | Машиночитаемый результат |
| `created_at` | `DateTime(timezone=True)` | Нет | Время постановки |
| `completed_at` | `DateTime(timezone=True)` | Да | Время завершения |

Ограничения и индексы:

- Primary key: `id`.
- Foreign key: `user_id -> users.id ON DELETE CASCADE`.
- Foreign key: `thread_id -> threads.id ON DELETE CASCADE`.
- Index: `ix_jobs_kind`.
- Index: `ix_jobs_status`.
- Index: `ix_jobs_thread_id`.
- Composite index: `ix_jobs_thread_kind_status`.
- Index: `ix_jobs_user_id`.

Структура `result_payload` зависит от типа задачи:

```json
{
  "task_id": "<conversion-job-id>",
  "items": [
    {
      "kind": "analytics",
      "filename": "report.pdf",
      "stem": "report",
      "document_id": "uuid-or-null",
      "error": "string-or-null"
    }
  ]
}
```

```json
{
  "task_id": "<generation-job-id>",
  "analysis_markdown": "# Аналитика ...",
  "assistant_message_id": "uuid-or-null"
}
```

### 4.8. Миграции

В репозитории зафиксированы две миграции:

1. `20260511_0001_multi_user_threads.py` создаёт базовые таблицы `users`, `threads`, `documents`, `messages`, `jobs`.
2. `20260512_0002_document_kinds_and_generation.py` добавляет поле `documents.kind`, индекс по нему и меняет уникальность документа с `(thread_id, stem)` на `(thread_id, kind, stem)`.

## 5. Аутентификация и изоляция доступа

### 5.1. Механизм сессии

Сервис реализует cookie-based session поверх JWT:

- при логине и регистрации создаётся JWT с payload `{sub, email, exp}`;
- токен подписывается алгоритмом `HS256`;
- cookie выставляется функцией `set_session_cookie()` с параметрами:
  - `httponly=True`;
  - `samesite="lax"`;
  - `secure=settings.AUTH_COOKIE_SECURE`;
  - `path="/"`;
  - `max_age=settings.AUTH_SESSION_TTL_SECONDS`.

### 5.2. Хеширование паролей

Пароли хешируются через `passlib` с использованием схемы `pbkdf2_sha256`. В комментарии к коду явно указано, что `bcrypt` не используется для новых хешей из-за возможных runtime-проблем в контейнерной среде.

### 5.3. Нормализация и валидация пользователя

- Email приводится к нижнему регистру и очищается от крайних пробелов через `normalize_email()`.
- Помимо ограничений Pydantic выполняется дополнительная проверка наличия символа `@`, а также отсутствие `@` в начале и конце строки.
- `get_current_user()` извлекает cookie, декодирует JWT и поднимает `401`, если:
  - cookie отсутствует;
  - токен недействителен;
  - пользователь из токена не найден в БД.

### 5.4. Изоляция по пользователю

Все ресурсы сервиса изолированы по владельцу:

- тред извлекается только по комбинации `(thread_id, user_id)`;
- задачи статуса извлекаются только по `(task_id, user_id, thread_id, kind)`;
- любой документ всегда принадлежит треду, а тред принадлежит пользователю;
- пользователь не может читать, изменять или удалять чужие треды и связанные документы.

## 6. HTTP-интерфейс

### 6.1. Не-API маршруты

| Маршрут | Авторизация | Вход | Успех | Ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `GET /` | Нет | Нет | `200`, `index.html` | файловые ошибки не перехватываются явно | Нет |
| `GET /static/*` | Нет | Путь к статическому ресурсу | `200`, файл | `404`, если файла нет | Нет |

### 6.2. Группа `auth`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `POST /api/v1/auth/register` | Нет | `AuthRegisterRequest` | `201`, `AuthUserResponse` | `409`, если email уже существует; `422`, если не пройдена валидация схемы | Создание `users`, установка cookie |
| `POST /api/v1/auth/login` | Нет | `AuthLoginRequest` | `200`, `AuthUserResponse` | `401`, если неверный email или пароль; `422`, если не пройдена валидация схемы | Установка cookie |
| `POST /api/v1/auth/logout` | Нет | Нет | `204` | специальных ошибок нет | Удаление cookie |
| `GET /api/v1/auth/me` | Да | Нет | `200`, `AuthUserResponse` | `401`, если сессия отсутствует/недействительна | Нет |

### 6.3. Группа `threads`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `GET /api/v1/threads` | Да | Нет | `200`, `list[ThreadResponse]` | `401` | Нет |
| `POST /api/v1/threads` | Да | `ThreadCreateRequest` | `201`, `ThreadResponse` | `401`, `422` | Запись в БД, создание каталогов треда |
| `GET /api/v1/threads/{thread_id}` | Да | Path `thread_id` | `200`, `ThreadResponse` | `401`, `404` | Нет |
| `PATCH /api/v1/threads/{thread_id}` | Да | `ThreadUpdateRequest` | `200`, `ThreadResponse` | `400`, если после `strip()` заголовок пуст; `401`; `404`; `422` | Обновление названия и `updated_at` |
| `DELETE /api/v1/threads/{thread_id}` | Да | Path `thread_id` | `204` | `401`; `404`; `409`, если по треду есть активные job | Удаление треда из БД и его файлового хранилища |

### 6.4. Группа `documents`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `GET /api/v1/threads/{thread_id}/documents` | Да | Path `thread_id` | `200`, `list[DocumentResponse]` | `401`, `404` | Нет |
| `DELETE /api/v1/threads/{thread_id}/documents/{document_id}` | Да | Path `thread_id`, `document_id` | `204` | `401`; `404`; `409`, если есть активные job по треду | Удаление записи документа, очистка файлов и обновление `thread.updated_at` |

### 6.5. Группа `conversions`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `POST /api/v1/threads/{thread_id}/conversions` | Да | `multipart/form-data`: `document_kind`, `files[]` | `202`, `ConversionCreateResponse` | `400` при пустом наборе или неподдерживаемом файле; `401`; `404`; `409` при активной conversion-задаче, дублирующих stem в batch, конфликте с существующим выводом или Redis lock; `422` при отсутствующих form fields | Создание `Job`, резервирование stem, сериализация файлов в payload и постановка Celery-задачи |
| `GET /api/v1/threads/{thread_id}/conversions/{task_id}` | Да | Path `thread_id`, `task_id` | `200`, `ConversionStatusResponse` | `401`; `404` треда или задачи | Нет |

Дополнительные бизнес-правила `POST /conversions`:

- `document_kind` после `strip().lower()` должен быть только `analytics` или `sources`;
- поддерживаются только расширения `.pdf`, `.html`, `.htm`;
- имя файла нормализуется через `Path(filename).name`;
- `stem` должен быть непустым после удаления расширения и пробелов;
- одинаковые `stem` внутри одной batch-загрузки запрещены;
- одинаковые `stem` в БД и в файловом хранилище проверяются до постановки задачи;
- в рамках одного треда одновременно разрешена только одна активная conversion-задача.

### 6.6. Группа `messages`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `GET /api/v1/threads/{thread_id}/messages` | Да | Path `thread_id` | `200`, `list[MessageResponse]` | `401`, `404` | Нет |

### 6.7. Группа `generations`

| Маршрут | Авторизация | Request | Success response | Основные ошибки | Побочные эффекты |
| --- | --- | --- | --- | --- | --- |
| `POST /api/v1/threads/{thread_id}/generations` | Да | `GenerationRequest` | `202`, `GenerationCreateResponse` | `400`, если `period_description` пуст после `strip()`; `401`; `404`; `409`, если уже есть активная generation-задача или в треде нет хотя бы одного документа каждого вида; `422` при ошибке схемы | Создание user-message, создание `generation` job и постановка Celery-задачи |
| `GET /api/v1/threads/{thread_id}/generations/{task_id}` | Да | Path `thread_id`, `task_id` | `200`, `GenerationStatusResponse` | `401`; `404` треда или задачи | Нет |

Ключевое бизнес-правило генерации:

- генерация не стартует, пока в треде не появится хотя бы один `analytics`-документ и хотя бы один `sources`-документ.

## 7. Pydantic-схемы API

Ниже перечислены все схемы, используемые HTTP-слоем. После них приведены ключевые внутренние typed-схемы, которые связывают auth, preprocessing, LLM-вызовы и generation runner.

### 7.1. `AuthRegisterRequest`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `email` | `str` | Да | `min_length=3`, `max_length=320` | Email пользователя |
| `password` | `str` | Да | `min_length=8`, `max_length=128` | Пароль для регистрации |

### 7.2. `AuthLoginRequest`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `email` | `str` | Да | `min_length=3`, `max_length=320` | Email пользователя |
| `password` | `str` | Да | `min_length=1`, `max_length=128` | Пароль для входа |

### 7.3. `AuthUserResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `id` | `str` | Да | нет | UUID пользователя |
| `email` | `str` | Да | нет | Нормализованный email |
| `created_at` | `datetime` | Да | timezone-aware | Время создания пользователя |

### 7.4. `ThreadCreateRequest`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `title` | `str \| null` | Нет | `max_length=255` | Начальное имя треда; если пусто или только пробелы, заменяется на `Новый тред` |

### 7.5. `ThreadUpdateRequest`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `title` | `str` | Да | `min_length=1`, `max_length=255` | Новое имя треда; после `strip()` не должно быть пустым |

### 7.6. `ThreadResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `id` | `str` | Да | нет | UUID треда |
| `title` | `str` | Да | нет | Текущее имя |
| `created_at` | `datetime` | Да | timezone-aware | Время создания |
| `updated_at` | `datetime` | Да | timezone-aware | Последнее изменение |

### 7.7. `DocumentResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `id` | `str` | Да | нет | UUID документа |
| `kind` | `str` | Да | фактически `analytics` или `sources` | Вид документа |
| `original_filename` | `str` | Да | нет | Имя загруженного файла |
| `stem` | `str` | Да | нет | Базовое имя без расширения |
| `created_at` | `datetime` | Да | timezone-aware | Время сохранения записи |

### 7.8. `MessageResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `id` | `str` | Да | нет | UUID сообщения |
| `role` | `str` | Да | фактически `user` или `assistant` | Роль автора |
| `content` | `str` | Да | нет | Текст периода или Markdown-аналитика |
| `created_at` | `datetime` | Да | timezone-aware | Время добавления |

### 7.9. `ConversionAcceptedFile`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `kind` | `str` | Да | `analytics` или `sources` | Вид принятого документа |
| `filename` | `str` | Да | нет | Имя файла |
| `stem` | `str` | Да | нет | Нормализованный stem |

### 7.10. `ConversionCreateResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `task_id` | `str` | Да | нет | UUID задачи конвертации |
| `status_url` | `str` | Да | URL | Ссылка на polling статуса |
| `files` | `list[ConversionAcceptedFile]` | Да | не пустой список для успешного запроса | Подтверждённый состав batch |

### 7.11. `ConversionItemResult`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `kind` | `str` | Да | `analytics` или `sources` | Вид документа |
| `filename` | `str` | Да | нет | Исходное имя |
| `stem` | `str` | Да | нет | Stem |
| `document_id` | `str \| null` | Нет | UUID или `null` | Созданный документ при успехе |
| `error` | `str \| null` | Нет | текст или `null` | Ошибка файла, rollback или skip |

### 7.12. `ConversionStatusResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `task_id` | `str` | Да | нет | UUID задачи |
| `status` | `str` | Да | один из job status | Текущий статус |
| `items` | `list[ConversionItemResult]` | Да | по умолчанию пустой список | Результаты по файлам |
| `error` | `str \| null` | Нет | текст или `null` | Общая ошибка batch |

### 7.13. `GenerationRequest`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `period_description` | `str` | Да | `min_length=1`, `max_length=10000` | Целевой период генерации |

### 7.14. `GenerationCreateResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `task_id` | `str` | Да | нет | UUID generation-задачи |
| `status_url` | `str` | Да | URL | Ссылка для polling |
| `user_message_id` | `str` | Да | UUID | Идентификатор сохранённого user-message |

### 7.15. `GenerationStatusResponse`

| Поле | Тип | Обязательное | Ограничения | Смысл |
| --- | --- | --- | --- | --- |
| `task_id` | `str` | Да | нет | UUID задачи |
| `status` | `str` | Да | один из job status | Текущий статус |
| `analysis_markdown` | `str \| null` | Нет | текст Markdown или `null` | Итоговая аналитика |
| `error` | `str \| null` | Нет | текст или `null` | Текст ошибки |
| `assistant_message_id` | `str \| null` | Нет | UUID или `null` | Идентификатор assistant-message |

### 7.16. Внутренние typed-схемы сервиса

#### `SessionTokenPayload`

Используется в `app/auth.py` после декодирования JWT.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `sub` | `str` | UUID пользователя |
| `email` | `str` | Email из токена |
| `exp` | `int` | Unix timestamp истечения сессии |

#### `ContentType`, `Role`, `Content`

Используются в `app/schema.py` и `app/llm_utils.py` для сборки multimodal-сообщений в OpenAI-compatible формате.

`Role`:

- `user`
- `assistant`
- `system`

`ContentType`:

- `text`
- `image_url`

`Content`:

| Поле | Тип | Смысл |
| --- | --- | --- |
| `value` | `str` | Текст или URL/data URL |
| `type` | `ContentType` | Тип элемента контента |

#### `Markdown`

Возвращается из `make_markdown()` и фиксирует конечные артефакты preprocessing.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `markdown_path` | `Path` | Путь к итоговому Markdown |
| `images_dir_path` | `Path` | Путь к каталогу картинок |

#### `ImageKindDetection`

Структура ответа модели на этапе классификации изображения.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `kind` | `Literal["chart", "other"]` | Тип изображения |
| `description` | `str \| null` | Краткое описание изображения |

Нормализация:

- `description` очищается от пробелов;
- пустая строка превращается в `null`.

#### `ImageAnalysis`

Структура ответа модели на этапе попытки восстановить таблицу из графика.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `kind` | `Literal["chart", "other"]` | Тип контента |
| `title` | `str \| null` | Заголовок графика |
| `description` | `str \| null` | Краткое описание |
| `columns` | `list[str]` | Заголовки колонок будущей таблицы |
| `rows` | `list[list[str]]` | Данные таблицы |
| `approximate` | `bool` | Признак приблизительности |

Валидаторы этой модели:

- удаляют пустые значения из `columns`;
- нормализуют строки в `rows`;
- отбрасывают полностью пустые строки;
- позволяют определить через `can_render_chart_table()`, можно ли безопасно собрать Markdown-таблицу.

#### `GenerationOutput`

Используется для валидации JSON-файла `.generation-result.json`, записанного `Codex`.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `status_message` | `str` | Краткое текстовое сообщение о завершении генерации |

#### `CodexTraceStep`

Внутренняя typed-модель шага generation runner.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `step_type` | `Literal["reasoning", "exec", "final_output"]` | Тип шага |
| `name` | `str` | Имя шага |
| `start_time` | `datetime` | Начало шага |
| `end_time` | `datetime` | Конец шага |
| `input` | `Any \| None` | Вход шага |
| `output` | `Any \| None` | Выход шага |
| `metadata` | `dict[str, Any]` | Дополнительные диагностические поля |
| `level` | `str \| None` | Уровень ошибки/важности |

#### `GenerationCodexRunResult`

Возвращается из `run_generation_codex_in_docker()`.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `analysis_markdown` | `str` | Итоговый Markdown анализ |
| `runner_container_name` | `str \| None` | Имя одноразового контейнера |
| `codex_session_id` | `str \| None` | Идентификатор сессии `Codex`, если извлечён из логов |
| `codex_model` | `str \| None` | Имя модели, если извлечено из логов |
| `steps` | `list[CodexTraceStep]` | Разобранные шаги выполнения |

## 8. Конвейер конвертации документов

### 8.1. Общий сценарий

```mermaid
sequenceDiagram
    participant UI as Browser/UI
    participant API as FastAPI
    participant DB as PostgreSQL
    participant R as Redis
    participant Q as Celery
    participant W as Worker
    participant TMP as Temp dir
    participant FS as Markdown storage

    UI->>API: POST /conversions (multipart)
    API->>API: Validate kind/files/stems
    API->>DB: Check thread ownership
    API->>DB: Check active conversion jobs
    API->>DB: Check DB/file conflicts
    API->>DB: Insert Job(status=queued)
    API->>R: Reserve stems via Lua script
    API->>Q: apply_async(...)
    Q->>W: convert_document_batch
    W->>DB: status=running
    W->>TMP: Materialize files from base64
    W->>FS: make_markdown()
    W->>DB: Insert Document rows
    W->>DB: status=completed + result_payload
    W->>R: Release stems
    UI->>API: GET /conversions/{task_id}
    API-->>UI: status/items/error
```

### 8.2. Валидация на стороне API

При создании conversion-задачи HTTP-слой выполняет несколько последовательных проверок:

1. Проверяет корректность `document_kind`.
2. Проверяет, что список `files` не пуст.
3. Проверяет расширения файлов по `suffix.lower()`.
4. Преобразует каждое имя к `Path(filename).name`, тем самым отбрасывая клиентские пути.
5. Извлекает `stem` и отклоняет пустые значения.
6. Проверяет отсутствие дублей `stem` внутри одного batch.
7. Проверяет существование треда и владение им.
8. Проверяет отсутствие другой активной conversion-задачи по данному треду.
9. Проверяет отсутствие конфликтов как в БД, так и в файловом хранилище (`.md`, `*_images`, `*_artifacts`).

Проверка конфликтов выполняется до постановки задачи в очередь. Это уменьшает вероятность длинных фоновых конфликтов и даёт пользователю синхронную диагностику.

### 8.3. Redis-резервирование stem

Для защиты от конкурентных гонок между несколькими запросами используется `Redis` и пара Lua-скриптов:

- `_RESERVE_STEMS_SCRIPT` атомарно проверяет отсутствие ключей и затем создаёт ключи блокировки;
- `_RELEASE_STEMS_SCRIPT` удаляет только те ключи, значение которых совпадает с `owner`.

Формат ключа:

```text
financial_analytics:thread_stem_lock:{thread_id}:{kind}:{stem}
```

Логика резервирования:

- если хотя бы один ключ уже существует, новый batch отклоняется с `409`;
- если все ключи свободны, они создаются со временем жизни `STEM_LOCK_TTL_SECONDS`;
- `owner` в текущей реализации равен `task_id`.

### 8.4. Сериализация payload для Celery

Файлы не записываются во временное пользовательское хранилище до постановки задачи. Вместо этого API:

1. читает `UploadFile` в память;
2. вычисляет SHA-256 для логирования;
3. кодирует байты в `base64`;
4. передаёт содержимое вместе с метаданными в payload Celery-задачи.

Это означает, что текущий production flow не использует каталог `uploaded_pdfs/` для передачи файлов в worker. Данный каталог создаётся storage-слоем, но в активном кодовом пути не является основным источником входных документов.

### 8.5. Выполнение `convert_document_batch`

Задача `convert_document_batch` реализует batch-семантику: либо весь пакет считается успешно проведённым, либо при критической ошибке результаты пакета откатываются.

Последовательность работы воркера:

1. Устанавливает `Job.status = running`.
2. Создаёт временный каталог `TemporaryDirectory`.
3. Для каждого файла materialize-ит содержимое из `base64` в локальный файл.
4. Запускает `make_markdown(source_path, output_dir=thread_documents_dir(...))`.
5. Накапливает результирующие метаданные будущих `Document`.
6. При успехе всех файлов открывает транзакцию БД и создаёт строки `documents`.
7. Обновляет `thread.updated_at`.
8. Сохраняет `result_payload` и ставит статус `completed`.

### 8.6. Семантика ошибок и rollback

Если ошибка возникает при обработке одного файла внутри batch:

- worker вызывает `_rollback_batch_outputs()` для всех `stem` пакета;
- ранее успешные элементы получают текст ошибки `Rolled back because another file in this batch failed.`;
- ещё не начатые элементы получают `Skipped because batch processing already failed.`;
- `Job.status` переводится в `failed`;
- stem-локи освобождаются в `finally`.

Следовательно, модель обработки пакета является атомарной по результирующему пользовательскому состоянию, хотя физически часть промежуточной работы могла быть выполнена до rollback.

### 8.7. Внутренний pipeline `make_markdown`

Функция `make_markdown()` реализует многошаговое преобразование:

| Этап | Действие |
| --- | --- |
| 1 | Формирует пути `stem.md`, `stem_images/`, `stem_artifacts/` |
| 2 | Конвертирует документ через `Docling` в Markdown с `ImageRefMode.REFERENCED` |
| 3 | Читает Markdown и оценивает качество текстового извлечения |
| 4 | При `ocr_mode="auto"` и признаках плохого извлечения повторяет конвертацию с OCR |
| 5 | Ищет строки вида `![Image](...)` |
| 6 | Для каждой картинки собирает контекст до и после изображения |
| 7 | Асинхронно классифицирует картинку как график или иной тип |
| 8 | Для графиков пытается восстановить таблицу, для остальных строит описание |
| 9 | Удаляет декоративные изображения |
| 10 | Перенумеровывает и копирует значимые изображения в итоговый каталог |
| 11 | Чистит Markdown от артефактов списков и лишних пустых строк |
| 12 | Удаляет неиспользуемые изображения и временные артефакты |

### 8.8. OCR и эвристики качества текста

OCR включается в одном из двух случаев:

- принудительно, если `ocr_mode="force"`;
- автоматически, если:
  - число содержательных слов в Markdown мало;
  - либо текст содержит подозрительно много placeholder-символов маркеров списка.

Конкретная эвристика `_looks_like_broken_text_extraction()`:

- `meaningful_words <= 120`, либо
- `meaningful_words <= 300` и `bullet_placeholders >= 8`.

Поддерживаемые OCR backend:

- `rapidocr`, если доступен `onnxruntime`;
- `tesseract`, если в системе есть бинарник `tesseract`.

### 8.9. Анализ изображений внутри документа

В preprocessing используются три модели:

| Назначение | Модель |
| --- | --- |
| Классификация график/не график | `qwen/qwen3-vl-32b-instruct` |
| Извлечение таблицы из графика | `openai/gpt-5.4` |
| Краткое описание обычного изображения | `qwen/qwen3-vl-32b-instruct` |

Правила обработки изображений:

- графики преобразуются в Markdown-таблицу;
- если таблицу восстановить нельзя, система пытается оставить описание;
- декоративные элементы удаляются совсем;
- итоговое изображение вставляется как:

```markdown
![Image](<images_dir>/<n>.png)

Краткое описание: ...
```

Для графиков дополнительно добавляется служебное примечание:

```markdown
> Значения графика перенесены в таблицу приблизительно, по визуальной оценке.
```

### 8.10. Файловая структура после конвертации

Пример результата для одного треда:

```text
markdowns/
  threads/
    <thread_id>/
      аналитика/
        report_q1_2025.md
        report_q1_2025_images/
          0.png
          1.png
      документы/
        press_release_april_2026.md
        press_release_april_2026_images/
          0.png
```

## 9. Конвейер генерации новой аналитики

### 9.1. Общий сценарий

```mermaid
sequenceDiagram
    participant UI as Browser/UI
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Celery
    participant W as Worker
    participant FS as Markdown volume
    participant D as Docker daemon
    participant C as Codex container
    participant LF as Langfuse

    UI->>API: POST /generations
    API->>DB: Validate thread and document kinds
    API->>DB: Insert user message + generation job
    API->>Q: apply_async(...)
    Q->>W: generate_analytics_by_period
    W->>DB: status=running
    W->>DB: Wait until no active conversion jobs
    W->>LF: Start root observation
    W->>D: Create workspace volume + copy container
    D->>FS: Snapshot threads/<thread_id> into /workspace
    W->>D: Run Codex runner container
    C->>C: Generate analysis in /workspace/.generated-analysis.md
    C->>C: Write /workspace/.generation-result.json
    W->>D: Read result files and logs
    W->>DB: Insert assistant message
    W->>DB: status=completed + payload
    W->>LF: Emit step observations, flush
    UI->>API: GET /generations/{task_id}
    API-->>UI: analysis_markdown / status
```

### 9.2. Проверки на стороне API

Перед постановкой generation-задачи API выполняет:

1. `strip()` для `period_description`;
2. проверку на пустое значение после обрезки пробелов;
3. проверку владения тредом;
4. проверку отсутствия другой активной generation-задачи;
5. проверку наличия обоих `document_kind` в треде.

Только после этого:

- создаётся сообщение `role="user"` с текстом периода;
- создаётся строка `jobs` со статусом `queued`;
- оба объекта коммитятся в БД в рамках одного запроса.

### 9.3. Выполнение `generate_analytics_by_period`

Worker выполняет следующие шаги:

1. Переводит job в `running`.
2. Открывает tracing context для `Langfuse`, если он включён.
3. Вызывает `_wait_for_no_active_conversions(thread_id)`.
4. Запускает `run_generation_codex_in_docker(...)`.
5. После получения результата создаёт `assistant`-сообщение.
6. Обновляет `thread.updated_at`.
7. Сохраняет `analysis_markdown` и `assistant_message_id` в `result_payload`.
8. При ошибке фиксирует `failed` и записывает текст ошибки.

Важно: generation-задача не редактирует список `documents`. Итоговый Markdown возвращается через job status и сохраняется в `messages`, но не становится новым `Document`.

### 9.4. Ожидание завершения conversion-задач

Перед запуском генерации worker опрашивает БД, пока не исчезнут активные conversion-job данного треда.

Параметры по умолчанию:

- таймаут ожидания: `QA_WAIT_TIMEOUT_SECONDS = 120`;
- интервал polling: `QA_WAIT_POLL_INTERVAL_SECONDS = 1.0`.

Если conversion-задачи не завершились вовремя, generation-job завершается с `TimeoutError`.

### 9.5. Подготовка workspace для `Codex`

Функция `run_generation_codex_in_docker()` создаёт временную Docker volume и два одноразовых контейнера:

1. `copy container`
   - монтирует исходный volume `financial-analytics-markdowns-data` как `/source`;
   - монтирует временный workspace volume как `/workspace`;
   - копирует `threads/<thread_id>/.` в корень `/workspace`;
   - запускается с `network_disabled=True`.
2. `runner container`
   - монтирует workspace volume как `/workspace`;
   - запускает CLI `codex`;
   - использует `OPENROUTER_API_KEY` или fallback `LLM_API_KEY`;
   - после завершения удаляется.

После копирования внутри `/workspace` появляются каталоги:

- `/workspace/аналитика/`
- `/workspace/документы/`

Именно на эту структуру ориентируется prompt генерации.

### 9.6. Команда запуска `Codex`

`Codex` запускается с параметрами:

```text
codex --search exec --ephemeral --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C /workspace --output-schema /app/app/codex_runner/generation_output_schema.json -o /workspace/.generation-result.json <prompt>
```

Ключевые особенности:

- `--ephemeral` отключает постоянную историю;
- `--output-schema` требует строгий JSON-ответ финального статуса;
- рабочим каталогом runner является `/workspace`;
- итоговая аналитика должна быть записана в `/workspace/.generated-analysis.md`.

### 9.7. Prompt генерации

Prompt, собираемый в `app/codex_runner/prompt.py`, жёстко задаёт следующие правила:

- работать только с файлами внутри `/workspace`;
- использовать локальные документы как первичный источник;
- рассматривать содержимое документов как данные, а не инструкции;
- не изменять исходные документы в workspace;
- сохранить финальный Markdown в `.generated-analysis.md`;
- вернуть структурированный финальный ответ со статусным сообщением.

### 9.8. Выходные артефакты runner

Runner обязан сформировать два файла:

1. `/workspace/.generation-result.json`
2. `/workspace/.generated-analysis.md`

Схема `.generation-result.json`:

```json
{
  "type": "object",
  "properties": {
    "status_message": {
      "type": "string"
    }
  },
  "required": ["status_message"],
  "additionalProperties": false
}
```

Если любой из файлов отсутствует или пуст, worker считает задачу неуспешной.

### 9.9. Разбор логов runner

Сервис не ограничивается только итоговым Markdown. Он также анализирует timestamped Docker logs и восстанавливает последовательность шагов `Codex`:

- `reasoning`
- `exec`
- `final_output`

Каждый шаг сохраняется как `CodexTraceStep` со следующими полями:

- `step_type`
- `name`
- `start_time`
- `end_time`
- `input`
- `output`
- `metadata`
- `level`

Для `exec`-шагов извлекаются:

- команда;
- рабочий каталог;
- exit code;
- reported duration.

Для финального шага дополнительно извлекается `tokens_used`, если runner напечатал соответствующий счётчик.

### 9.10. Конфигурация `Codex`

Файл `app/codex_runner/config.toml` задаёт:

- `model_provider = "openrouter"`
- `model = "gpt-5.4"`
- `model_reasoning_effort = "xhigh"`
- `plan_mode_reasoning_effort = "xhigh"`
- `[history] persistence = "none"`

Следовательно, генерация ориентирована на модель `gpt-5.4`, а история сессии внутри runner намеренно не сохраняется между запусками.

## 10. Хранилище состояния и файлов

### 10.1. Каталоги и именование

Функции в `app/storage.py` определяют канонические пути:

| Функция | Результат |
| --- | --- |
| `thread_markdowns_dir(thread_id)` | `MARKDOWNS_ROOT/threads/<thread_id>` |
| `thread_uploads_dir(thread_id)` | `UPLOADED_PDFS_ROOT/threads/<thread_id>` |
| `thread_documents_dir(thread_id, kind)` | каталог вида `.../аналитика` или `.../документы` |
| `thread_markdown_path(thread_id, kind, stem)` | `<stem>.md` |
| `thread_images_dir(thread_id, kind, stem)` | `<stem>_images/` |
| `thread_artifacts_dir(thread_id, kind, stem)` | `<stem>_artifacts/` |

### 10.2. Особенности `uploaded_pdfs`

Каталог `UPLOADED_PDFS_DIR`:

- создаётся при инициализации storage-слоя;
- создаётся для каждого треда в `ensure_thread_dirs()`;
- удаляется вместе с тредом в `delete_thread_storage()`.

Однако в текущем HTTP->Celery потоке исходные файлы передаются через `base64` в payload задачи и materialize-ятся во временном каталоге worker. Поэтому `uploaded_pdfs` в текущей реализации выступает скорее как зарезервированная часть хранилища, а не как активный transport layer.

### 10.3. Согласованность БД и файловой системы

Сервис не использует распределённые транзакции между БД и файловой системой.

Практические последствия:

- тред сначала коммитится в БД, затем создаются каталоги;
- документ сначала удаляется из БД, затем удаляются файлы;
- при удалении треда сначала удаляется запись, затем очищается storage;
- корректность обеспечивается последовательностью операций и cleanup-функциями, а не двухфазным commit.

### 10.4. Машина состояний job

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running
    running --> completed
    running --> failed
    queued --> failed
```

Переходы:

- `queued -> running`: внутри worker через `_set_job_running()`;
- `running -> completed`: после успешного завершения операции;
- `running -> failed`: при исключении;
- `queued -> failed`: возможен при исключениях до нормального старта тела работы.

## 11. Встроенный web-интерфейс

### 11.1. Общая характеристика

Frontend реализован как лёгкое SPA на нативном JavaScript без отдельного фреймворка. Интерфейс:

- загружается из `GET /`;
- обращается к тому же origin, что и API;
- использует cookie-сессию браузера;
- не применяет WebSocket или SSE;
- получает прогресс длительных задач через polling каждые 2 секунды.

### 11.2. Структура клиентского состояния

В `app/web/app.js` есть единый объект `state` со следующими полями:

- `authMode`
- `user`
- `threads`
- `activeThreadId`
- `activeThread`
- `documents`
- `messages`
- `conversionPollId`
- `generationPollId`

### 11.3. Поведение интерфейса

| Функциональность | Реализация |
| --- | --- |
| Автоматическое восстановление сессии | `boot()` вызывает `GET /api/v1/auth/me` |
| Создание треда | `POST /api/v1/threads` |
| Переименование треда | `PATCH /api/v1/threads/{id}` |
| Удаление треда | `DELETE /api/v1/threads/{id}` |
| Загрузка analytics | `POST /conversions` с `document_kind=analytics` |
| Загрузка sources | `POST /conversions` с `document_kind=sources` |
| Удаление документа | `DELETE /documents/{document_id}` |
| Запуск генерации | `POST /generations` |
| Polling конвертации | `GET /conversions/{task_id}` |
| Polling генерации | `GET /generations/{task_id}` |

### 11.4. Ограничения UI

Интерфейс содержит несколько важных инженерных особенностей:

- Markdown сгенерированной аналитики отображается как обычный текст через `textContent`, а не рендерится как HTML/Markdown;
- документальные группы отображаются по `kind` в фиксированном порядке `analytics`, затем `sources`;
- список сообщений показывает label `Целевой период` для `user` и `Аналитика` для `assistant`;
- polling прекращается при переключении треда и при logout.

## 12. Конфигурация и развёртывание

### 12.1. Переменные окружения приложения

#### LLM и генерация

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `LLM_API_KEY` | нет | Базовый API-ключ для LLM-вызовов |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL |
| `OPENROUTER_API_KEY` | `null` | Отдельный ключ для `Codex` runner; при отсутствии используется `LLM_API_KEY` |

#### База данных и сессии

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://financial_analytics:financial_analytics@postgres:5432/financial_analytics` | Подключение к PostgreSQL |
| `AUTH_JWT_SECRET` | `dev-only-change-me-please-rotate-32chars` | Секрет подписи JWT |
| `AUTH_COOKIE_NAME` | `fa_session` | Имя cookie сессии |
| `AUTH_COOKIE_SECURE` | `False` | Признак secure-cookie |
| `AUTH_SESSION_TTL_SECONDS` | `604800` | TTL сессии в секундах |

#### Storage и очереди

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `MARKDOWNS_DIR` | `markdowns` | Корень Markdown-хранилища |
| `UPLOADED_PDFS_DIR` | `uploaded_pdfs` | Корень reserved upload storage |
| `MARKDOWNS_DOCKER_VOLUME` | `financial-analytics-markdowns-data` | Имя volume для snapshot generation-workspace |
| `REDIS_URL` | `redis://redis:6379/2` | Redis для stem-lock |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Redis broker |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Redis result backend |
| `STEM_LOCK_TTL_SECONDS` | `3600` | TTL Redis-локов stem |
| `TASK_RETENTION_SECONDS` | `604800` | Зарезервированная настройка; напрямую в текущем коде не используется |

#### Таймауты runner

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `QA_WAIT_TIMEOUT_SECONDS` | `120` | Ожидание завершения conversion-job перед generation |
| `QA_WAIT_POLL_INTERVAL_SECONDS` | `1.0` | Частота polling активных conversion-job |
| `QA_RUNNER_IMAGE` | `financial-analytics:local` | Docker image для copy/runner контейнеров |
| `QA_CONTAINER_TIMEOUT_SECONDS` | `1800` | Таймаут выполнения generation runner |
| `QA_COPY_CONTAINER_TIMEOUT_SECONDS` | `120` | Таймаут подготовки snapshot workspace |

#### Langfuse

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `LANGFUSE_PUBLIC_KEY` | `null` | Публичный ключ Langfuse |
| `LANGFUSE_SECRET_KEY` | `null` | Секретный ключ Langfuse |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Базовый URL Langfuse |
| `LANGFUSE_TRACING_ENABLED` | `True` | Глобальный флаг трассировки |
| `LANGFUSE_TRACING_ENVIRONMENT` | `default` | Имя окружения |
| `LANGFUSE_RELEASE` | `null` | Метка релиза |

### 12.2. Dockerfile

`Dockerfile` формирует единый image для API, worker и generation runner:

- базируется на `python:3.12-slim`;
- устанавливает системные библиотеки, Node.js и npm;
- устанавливает `@openai/codex@latest` глобально;
- подтягивает `uv` из образа `ghcr.io/astral-sh/uv:latest`;
- выполняет `uv sync --no-dev`;
- копирует `app/codex_runner/config.toml` в `/root/.codex/config.toml`.

Таким образом, один и тот же образ используется:

- как runtime для FastAPI;
- как runtime для Celery worker;
- как базовый image для одноразовых контейнеров генерации.

### 12.3. Основной `docker-compose.yml`

Основной compose-стек поднимает пять сервисов:

| Сервис | Роль |
| --- | --- |
| `migrate` | Выполняет `alembic upgrade head` |
| `api` | Запускает `uvicorn app.main:app` |
| `worker` | Запускает `celery -A app.api.celery_app:celery_app worker --loglevel=INFO` |
| `postgres` | Хранит персистентные данные |
| `redis` | Используется как stem-lock store, broker и result backend |

Дополнительные особенности:

- `api` и `worker` монтируют volume `markdowns_data` и `uploaded_pdfs_data`;
- `worker` дополнительно монтирует `/var/run/docker.sock:/var/run/docker.sock`, так как сам запускает generation-контейнеры;
- в `api` и `worker` прокидывается `LANGFUSE_BASE_URL=${LANGFUSE_BASE_URL_DOCKER:-http://host.docker.internal:3000}`;
- `extra_hosts` содержит `host.docker.internal:host-gateway`, чтобы контейнеры могли достучаться до локально поднятого `Langfuse`.

### 12.4. Отдельный `docker-compose.langfuse.yml`

Для локального self-hosted наблюдения предусмотрен отдельный стек `Langfuse`, состоящий из:

- `langfuse-web`
- `langfuse-worker`
- `clickhouse`
- `minio`
- `langfuse-redis`
- `postgres`

Это позволяет развернуть полный локальный контур трассировки без зависимости от облачного `Langfuse`.

## 13. Наблюдаемость и диагностическая информация

### 13.1. Логирование

При обработке conversion-upload API логирует:

- `task_id`
- `thread_id`
- `filename`
- размер файла
- SHA-256 содержимого

Worker логирует:

- путь materialized source file;
- начало конвертации конкретного файла;
- исключения при conversion failure.

### 13.2. Langfuse

Трассировка активируется только если одновременно выполнены условия:

- `LANGFUSE_TRACING_ENABLED=true`;
- задан `LANGFUSE_PUBLIC_KEY`;
- задан `LANGFUSE_SECRET_KEY`.

Во время generation-flow в `Langfuse` отправляются:

- корневое наблюдение типа `agent`;
- метаданные trace: `user_id`, `session_id=task_id`, `thread_id`, `flow=generation`;
- детализированные шаги `Codex` как span/generation events;
- сведения о модели и расходе токенов, если их удалось извлечь из логов runner.

## 14. Подтверждённые тестами сценарии

В проекте присутствует файл `tests/test_api.py`. Тесты запускаются через стандартный `unittest`, используют временную SQLite-базу и patch-ят `Celery`-задачи синхронными заглушками.

Проверенные сценарии:

1. Один и тот же `stem` допускается в одном треде для разных `document_kind`, а generation-flow возвращает Markdown и создаёт два сообщения.
2. Повторная загрузка документа с тем же `stem` и тем же `kind` отклоняется с `409`.
3. Генерация запрещена, пока в треде не загружен хотя бы один `analytics`-документ и хотя бы один `sources`-документ.

Эти тесты не проверяют фактическую работу `Docling`, Docker runner или внешних LLM, но подтверждают корректность центральной бизнес-логики API и инвариантов модели данных.

## 15. Ограничения и текущие допущения

Текущая реализация имеет ряд принципиальных ограничений:

1. Поддерживаются только документы с расширениями `.pdf`, `.html`, `.htm`.
2. Одновременно на один тред разрешается только одна активная conversion-задача и одна активная generation-задача.
3. Передача исходных файлов в worker выполняется через `base64` внутри Celery payload, что увеличивает размер сообщений брокера при больших файлах.
4. Каталог `uploaded_pdfs/` подготовлен архитектурно, но не используется как основной канал доставки документа в worker.
5. Успешно сгенерированная аналитика сохраняется в `messages`, но автоматически не регистрируется как новый `Document`.
6. Между БД и файловой системой нет распределённой транзакции.
7. Валидация email минимальна и не заменяет полнофункциональную проверку адресов.
8. Без доступа worker к Docker daemon generation-flow невозможен.
9. Без внешнего LLM API невозможны:
   - анализ изображений в preprocessing;
   - генерация новой аналитики через `Codex`.
10. Значения `AUTH_JWT_SECRET` и других default-параметров рассчитаны на локальную разработку и не должны использоваться без ротации в production.

## 16. Итог

Сервис `app` реализует полный цикл интеллектуальной обработки финансовых документов:

- от управления пользователями и приватными тредами;
- через асинхронную подготовку структурированного Markdown-корпуса;
- до генерации новой аналитики по целевому периоду в изолированном Docker-runner.

Архитектура сервиса сочетает:

- классический REST API;
- реляционную модель данных;
- фоновые очереди задач;
- специализированный preprocessing документов;
- интеграцию с LLM;
- опциональную трассировку выполнения.

Именно эта комбинация делает `app` не просто веб-обвязкой над моделью, а самостоятельным прикладным сервисом оркестрации аналитического конвейера.
