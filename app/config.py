from dotenv import load_dotenv
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"
    LANGFUSE_TRACING_ENABLED: bool = True
    LANGFUSE_TRACING_ENVIRONMENT: str = "default"
    LANGFUSE_RELEASE: str | None = None

load_dotenv()
settings = Settings()
