from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import settings


class LLMClient:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    async def chat_completion(
        self,
        messages: list[dict],
        model: str,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        request_kwargs = {}
        if extra_body is not None:
            request_kwargs["extra_body"] = dict(extra_body)

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
