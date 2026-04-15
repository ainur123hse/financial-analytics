from typing import Any

from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
from openai import AsyncOpenAI as OpenAIAsyncOpenAI

from app.config import settings
from app.langfuse_client import get_langfuse_client


class LLMClient:
    def __init__(self) -> None:
        self._uses_langfuse = get_langfuse_client() is not None
        client_cls = LangfuseAsyncOpenAI if self._uses_langfuse else OpenAIAsyncOpenAI

        self._client = client_cls(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    async def chat_completion(
        self,
        messages: list[dict],
        model: str,
        extra_body: dict[str, Any] | None = None,
        langfuse_name: str | None = None,
        langfuse_metadata: dict[str, Any] | None = None,
    ) -> Any:
        request_kwargs = {}
        if extra_body is not None:
            request_kwargs["extra_body"] = dict(extra_body)
        if self._uses_langfuse and langfuse_name is not None:
            request_kwargs["name"] = langfuse_name
        if self._uses_langfuse and langfuse_metadata is not None:
            request_kwargs["metadata"] = dict(langfuse_metadata)

        return await self._client.chat.completions.create(
            model=model,
            messages=messages,
            **request_kwargs,
        )

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
