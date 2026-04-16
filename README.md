# financial-analytics

FastAPI service with async PDF -> Markdown conversion and QA over generated markdowns.

## Run locally

```bash
uv venv .venv
source .venv/bin/activate
uv sync
uvicorn app.main:app --reload
```

Run Celery worker in a second terminal:

```bash
celery -A app.api.celery_app:celery_app worker --loglevel=INFO
```

You also need Redis (defaults in `.env` / `app/config.py`).

## Run with Docker Compose

```bash
docker compose up --build
```

This starts:
- `api` on `http://localhost:8000`
- `worker` for background conversion
- dedicated `redis`
- web UI on `http://localhost:8000/`

## API

### 1) Upload PDFs for conversion

`POST /api/v1/conversions` (multipart/form-data)
- field: `files` (one or many PDF files)
- returns: `task_id`, `status_url`, accepted files list

### 2) Check conversion status

`GET /api/v1/conversions/{task_id}`
- returns: `queued | running | completed | failed`
- includes per-file conversion result metadata

### 3) Ask question by markdowns

`POST /api/v1/qa`

```json
{
  "question": "Какая динамика выручки?"
}
```

Response:

```json
{
  "answer": "..."
}
```

Before QA, API waits up to 120 seconds until all active conversion tasks finish.

## Troubleshooting

If conversion fails with `libxcb.so.1: cannot open shared object file`, rebuild containers after pulling latest changes:

```bash
docker compose down
docker compose up --build
```
