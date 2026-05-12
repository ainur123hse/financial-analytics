FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libxcb1 \
       libx11-6 \
       libxext6 \
       libxrender1 \
       libsm6 \
       libglib2.0-0 \
       libgl1 \
       nodejs \
       npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm i -g @openai/codex@latest

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

COPY . .

RUN mkdir -p /root/.codex \
    && cp /app/app/codex_runner/config.toml /root/.codex/config.toml

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
