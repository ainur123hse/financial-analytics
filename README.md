# Financial Analytics

`financial-analytics` - сервис для генерации финансовой аналитики по корпоративным документам. Базовый сценарий работы: загрузить историческую аналитику и первичные источники, преобразовать их в Markdown и сгенерировать новую аналитическую записку по целевому периоду.

Репозиторий объединяет прикладной сервис, встроенный web UI, pipeline предобработки документов и benchmark-инструменты для восстановления аналитических отчетов по историческим периодам. Основной стек: `FastAPI + PostgreSQL + Redis + Celery + Docling + Dockerized Codex`, с опциональным tracing через `Langfuse`.

## Возможности

- регистрация и вход по email/password;
- треды с изоляцией пользовательских данных;
- загрузка документов двух типов: `analytics` и `sources`;
- асинхронная конвертация `PDF/HTML` в Markdown;
- генерация новой аналитики по свободному описанию целевого периода;
- benchmark-пайплайн для восстановления аналитики по историческим периодам.

## Быстрый запуск через Docker Compose

### Что понадобится

- `Docker` и `Docker Compose`;
- доступ к `/var/run/docker.sock` для сервиса `worker`;
- API key для LLM.

### Переменные окружения

Подготовьте `.env` в корне проекта. Минимально достаточно:

```env
LLM_API_KEY=<your_llm_api_key>
OPENROUTER_API_KEY=<optional_if_needed>
LANGFUSE_PUBLIC_KEY=<optional>
LANGFUSE_SECRET_KEY=<optional>
LANGFUSE_BASE_URL=<optional_for_local_runs>
LANGFUSE_BASE_URL_DOCKER=<optional_for_compose_runs>
```

Что важно:

- `LLM_API_KEY` обязателен: он нужен приложению для multimodal-обработки документов.
- `OPENROUTER_API_KEY` нужен `Codex`-runner и benchmark-скриптам, но если он не задан, часть скриптов автоматически подставляет туда `LLM_API_KEY`.
- `LANGFUSE_*` переменные не обязательны. Без них сервис работает, но не отправляет tracing.
- Для `docker compose` используйте `LANGFUSE_BASE_URL_DOCKER`, если `Langfuse` должен быть доступен из контейнеров.

### Запуск

```bash
docker compose up --build
```

После старта будут подняты:

- `api` - `FastAPI`-приложение и встроенный web UI;
- `worker` - `Celery`-воркер для conversion/generation jobs;
- `migrate` - одноразовый `alembic upgrade head`;
- `api-postgres` - основная база данных;
- `redis` - broker/result backend и coordination store.

Полезные адреса:

- UI: `http://localhost:8000/`
- OpenAPI: `http://localhost:8000/docs`

## Как пользоваться сервисом

Основной сценарий в UI:

1. Зарегистрируйтесь или войдите.
2. Создайте новый тред.
3. Загрузите документы прошлых периодов в блок `Аналитика прошлых периодов`.
4. Загрузите первичные источники в блок `Релевантные источники`.
5. Дождитесь завершения conversion task.
6. Укажите целевой период в свободной форме.
7. Запустите генерацию и дождитесь результата.

Ограничения текущей реализации:

- генерация не стартует, пока в тред не загружен хотя бы один документ `analytics` и хотя бы один документ `sources`;
- для одного треда одновременно разрешена только одна активная conversion task;
- для одного треда одновременно разрешена только одна активная generation task;
- поддерживаются только файлы `.pdf`, `.html`, `.htm`.

Результат генерации возвращается как Markdown и сохраняется в историю сообщений треда.

## API и интерфейсы

Основные группы маршрутов доступны под `/api/v1`:

| Группа | Назначение |
| --- | --- |
| `auth` | регистрация, login, logout, `me` |
| `threads` | создание, переименование, удаление, список тредов |
| `documents` / `conversions` | загрузка документов, polling статуса conversion job, удаление документов |
| `messages` / `generations` | история сообщений и запуск генерации |

Подробные схемы запросов и ответов доступны через Swagger UI на `/docs`.

Кроме HTTP API, в репозитории есть два пользовательских CLI entrypoint:

- batch conversion:

```bash
uv run python -m app.documents_preprocessing.batch_cli <input_dir>
```

- benchmark runner:

```bash
./execute_benchmark/exec.sh <dataset_root>
```

## Локальный запуск без Compose

Этот путь удобен для разработки, если `PostgreSQL` и `Redis` уже подняты отдельно.

### Установка зависимостей

```bash
uv sync --no-dev
```

### Минимальные переменные окружения

```bash
export LLM_API_KEY=<your_llm_api_key>
export DATABASE_URL=postgresql+psycopg://financial_analytics:financial_analytics@localhost:5432/financial_analytics
export REDIS_URL=redis://localhost:6379/2
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/1
export AUTH_JWT_SECRET=<change-me>
```

### Миграции, API и worker

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run celery -A app.api.celery_app:celery_app worker --loglevel=INFO
```

При локальном запуске `LANGFUSE_BASE_URL` используется напрямую из `app/config.py`. Для benchmark и generation через `Codex` может понадобиться также `OPENROUTER_API_KEY`.

## Поддерживаемые форматы и артефакты

Входные форматы:

- `.pdf`
- `.html`
- `.htm`

После conversion pipeline сервис создает:

- Markdown-файл документа;
- директорию `<stem>_images` с извлеченными изображениями;
- директорию `<stem>_artifacts` с промежуточными артефактами.

OCR не является обязательной частью базового сценария, но поддерживается при наличии backend:

- `onnxruntime` -> `RapidOCR`
- `tesseract` binary -> `Tesseract`

## Структура проекта

```text
app/                 FastAPI, Celery tasks, preprocessing, Codex runner, web UI
alembic/             database migrations
dataset/             benchmark datasets and results
execute_benchmark/   tmux-based benchmark runner and helpers
markdowns/           detailed technical documentation
```

Ключевые подсистемы:

- `app/main.py` - точка входа `FastAPI` и встроенный web UI;
- `app/api/routes.py` - HTTP-маршруты сервиса;
- `app/api/tasks.py` - фоновые conversion/generation jobs;
- `app/documents_preprocessing/` - conversion pipeline в Markdown;
- `app/codex_runner/` - запуск генерации в отдельном Docker-контейнере с `Codex`.

## Benchmark и датасеты

Репозиторий включает benchmark для задачи восстановления аналитической записки по историческому периоду. На каждом шаге модели доступны:

- аналитика предыдущих периодов;
- только те источники, которые были доступны на момент целевого периода.

Базовая структура датасета:

```text
<dataset_root>/
  аналитика/
  источники/
  bench_info.json
  benchmark_results/
```

### Что понадобится

- `codex` CLI в `PATH` или через `--codex-bin`;
- `tmux`;
- `OPENROUTER_API_KEY` или `LLM_API_KEY`.

### Примеры запуска

Запуск всех периодов:

```bash
./execute_benchmark/exec.sh dataset/северсталь
```

Запуск выбранных периодов:

```bash
./execute_benchmark/exec.sh dataset/северсталь --period 11 --period 12
```

Создание tmux session без attach:

```bash
./execute_benchmark/exec.sh dataset/северсталь --detach
```

Особенности текущей реализации:

- за один запуск можно обрабатывать не более 4 периодов;
- staging-каталоги создаются в `/tmp/analyze/<run_id>/`;
- результаты пишутся в `<dataset_root>/benchmark_results/<run_id>/`;
- generation и evaluation выполняются последовательно внутри отдельных `tmux` pane.

## Наблюдаемость

Tracing через `Langfuse` опционален. Если заданы `LANGFUSE_PUBLIC_KEY` и `LANGFUSE_SECRET_KEY`, сервис отправляет данные о generation pipeline и шагах `Codex`-runner.

Для локального self-hosted контура есть отдельный compose-файл:

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Если сервис запускается в Docker, для связи с локальным `Langfuse` используйте `LANGFUSE_BASE_URL_DOCKER`.

## Дополнительная документация

- [Техническое описание сервиса](markdowns/app_service_techspec.md)
- [Пайплайн датасета и методика benchmark](markdowns/benchmark_dataset_and_benchmarking.md)
- [Детали benchmark runner](execute_benchmark/bench-pipeline.md)
- [Черновой pipeline сбора датасета](make_dataset_pipeline.md)
