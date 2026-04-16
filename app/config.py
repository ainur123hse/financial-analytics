from dotenv import load_dotenv
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"

    MARKDOWNS_DIR: str = "markdowns"
    UPLOADED_PDFS_DIR: str = "uploaded_pdfs"

    REDIS_URL: str = "redis://redis:6379/2"
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"
    STEM_LOCK_TTL_SECONDS: int = 3600
    TASK_RETENTION_SECONDS: int = 604800
    QA_WAIT_TIMEOUT_SECONDS: int = 120
    QA_WAIT_POLL_INTERVAL_SECONDS: float = 1.0

    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"
    LANGFUSE_TRACING_ENABLED: bool = True
    LANGFUSE_TRACING_ENVIRONMENT: str = "default"
    LANGFUSE_RELEASE: str | None = None

load_dotenv()
settings = Settings()
