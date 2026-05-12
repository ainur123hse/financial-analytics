# financial-analytics

FastAPI service with:
- multi-user authentication
- private user-owned threads
- thread-scoped document conversion to Markdown
- two business document kinds per thread: past analytics and relevant sources
- thread-scoped analytics generation by target period with saved history

## Run locally

Requirements:
- Python 3.12+
- PostgreSQL
- Redis

Environment variables can be provided through `.env`. Important settings:
- `DATABASE_URL`
- `AUTH_JWT_SECRET`
- `REDIS_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`

Install and run:

```bash
uv venv .venv
source .venv/bin/activate
uv sync
alembic upgrade head
uvicorn app.main:app --reload
```

Run Celery worker in a second terminal:

```bash
celery -A app.api.celery_app:celery_app worker --loglevel=INFO
```

## Run with Docker Compose

```bash
docker compose up --build
```

This starts:
- `postgres`
- `redis`
- `migrate` one-off service running `alembic upgrade head`
- `api` on `http://localhost:8000`
- `worker` for conversion and generation jobs

For a clean first start of the new multi-user version, use empty volumes:

```bash
docker compose down -v
docker compose up --build
```

## Web app flow

1. Register or log in.
2. Create a thread.
3. Upload past analytics documents into that thread.
4. Upload relevant source documents into the same thread.
5. Wait for conversion to finish.
6. Submit a free-form target period description and generate a new analytics document.

Each thread has:
- its own analytics and sources sets
- its own generation history
- private access limited to the owning user

## API

### Auth

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`

Auth uses an HTTP-only cookie session.

### Threads

- `GET /api/v1/threads`
- `POST /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `PATCH /api/v1/threads/{thread_id}`
- `DELETE /api/v1/threads/{thread_id}`

### Documents

- `GET /api/v1/threads/{thread_id}/documents`
- `DELETE /api/v1/threads/{thread_id}/documents/{document_id}`

### Conversion

- `POST /api/v1/threads/{thread_id}/conversions`
- `GET /api/v1/threads/{thread_id}/conversions/{task_id}`

`POST /conversions` requires multipart field `document_kind` with one of:
- `analytics`
- `sources`

### Messages and Generation

- `GET /api/v1/threads/{thread_id}/messages`
- `POST /api/v1/threads/{thread_id}/generations`
- `GET /api/v1/threads/{thread_id}/generations/{task_id}`

Generation is asynchronous. The worker waits only for active conversion jobs of the same thread, snapshots that thread’s markdown workspace, then runs Codex in a one-off Docker container. The workspace passed to Codex contains:
- `аналитика/` for converted past analytics
- `документы/` for converted relevant sources

Generated analytics is returned as Markdown in the task status payload and saved in message history, but it is not added back as a stored thread document automatically.

## Langfuse

If `LANGFUSE_TRACING_ENABLED=true` and both `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set, generation traces are exported to Langfuse.

Common variables:
- `LANGFUSE_BASE_URL`
- `LANGFUSE_BASE_URL_DOCKER`
- `LANGFUSE_TRACING_ENVIRONMENT`
- `LANGFUSE_RELEASE`

## Notes

- This change is breaking for previously converted thread workspaces. Start with clean `markdowns` and `uploaded_pdfs` volumes after upgrading.
- Current defaults are suitable only for local development; rotate `AUTH_JWT_SECRET` in real deployments.
