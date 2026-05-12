from dotenv import load_dotenv
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_API_KEY: str | None = None

    DATABASE_URL: str = (
        "postgresql+psycopg://financial_analytics:financial_analytics@postgres:5432/financial_analytics"
    )
    AUTH_JWT_SECRET: str = "dev-only-change-me-please-rotate-32chars"
    AUTH_COOKIE_NAME: str = "fa_session"
    AUTH_COOKIE_SECURE: bool = False
    AUTH_SESSION_TTL_SECONDS: int = 604800

    MARKDOWNS_DIR: str = "markdowns"
    UPLOADED_PDFS_DIR: str = "uploaded_pdfs"
    MARKDOWNS_DOCKER_VOLUME: str = "financial-analytics-markdowns-data"

    REDIS_URL: str = "redis://redis:6379/2"
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"
    STEM_LOCK_TTL_SECONDS: int = 3600
    TASK_RETENTION_SECONDS: int = 604800
    QA_WAIT_TIMEOUT_SECONDS: int = 120
    QA_WAIT_POLL_INTERVAL_SECONDS: float = 1.0
    QA_RUNNER_IMAGE: str = "financial-analytics:local"
    QA_CONTAINER_TIMEOUT_SECONDS: int = 1800
    QA_COPY_CONTAINER_TIMEOUT_SECONDS: int = 120

    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"
    LANGFUSE_TRACING_ENABLED: bool = True
    LANGFUSE_TRACING_ENVIRONMENT: str = "default"
    LANGFUSE_RELEASE: str | None = None


load_dotenv()
settings = Settings()
