import base64
import mimetypes
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import settings
from app.observability import (
    build_traced_user_content,
    extract_usage_details,
    sanitize_chat_messages,
    serialize_for_langfuse,
    serialize_openai_response,
    start_observation,
    update_observation,
)

class LLMClient:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    async def chat_completion(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        extra_body: dict[str, Any] | None = None,
        *,
        phase: str,
        iteration: int | None,
        traced_messages: list[dict[str, Any]] | None = None,
    ) -> Any:
        request_kwargs = {}
        if extra_body is not None:
            request_kwargs["extra_body"] = dict(extra_body)

        observation_input = {
            "messages": (
                traced_messages
                if traced_messages is not None
                else sanitize_chat_messages(serialize_for_langfuse(list(messages)))
            ),
            "extra_body": serialize_for_langfuse(request_kwargs.get("extra_body")),
        }
        observation_metadata: dict[str, Any] = {
            "message_count": len(messages),
            "phase": phase,
            "reasoning_enabled": bool((extra_body or {}).get("reasoning", {}).get("enabled")),
        }
        if iteration is not None:
            observation_metadata["iteration"] = iteration
        model_parameters = {
            "reasoning_enabled": bool((extra_body or {}).get("reasoning", {}).get("enabled")),
        }

        with start_observation(
            name="llm.chat_completion",
            as_type="generation",
            input=serialize_for_langfuse(observation_input),
            metadata=serialize_for_langfuse(observation_metadata),
            model=model,
            model_parameters=model_parameters,
        ) as observation:
            try:
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=list(messages),
                    **request_kwargs,
                )
            except Exception as exc:
                update_observation(
                    observation,
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

            update_observation(
                observation,
                output=serialize_openai_response(response),
                usage_details=extract_usage_details(response),
            )
            return response

    async def ask(
        self,
        history: list[dict],
        model: str,
        system_prompt: str,
        prompts_with_images: list[tuple[str, str | None]],
        *,
        phase: str,
        iteration: int | None,
        reasoning: bool = False
    ) -> str:

        system_message = {
            "role": "system",
            "content": system_prompt,
        }

        messages = []
        traced_messages = [serialize_for_langfuse(system_message)]
        for prompt_and_image in prompts_with_images:
            image_paths = None
            if prompt_and_image[1] is not None:
                image_paths = [prompt_and_image[1]]
            messages.append(
                {
                    "role": "user",
                    "content": self._build_user_content(
                        prompt=prompt_and_image[0],
                        image_paths=image_paths,
                    ),
                }
            )
            traced_messages.append(
                {
                    "role": "user",
                    "content": build_traced_user_content(
                        prompt=prompt_and_image[0],
                        image_paths=image_paths,
                    ),
                }
            )
        messages = [system_message] + history + messages
        traced_messages = [serialize_for_langfuse(system_message)] + sanitize_chat_messages(history) + serialize_for_langfuse(traced_messages[1:])
        extra_body = {"reasoning": {"enabled": reasoning}}
        response = await self.chat_completion(
            messages,
            model=model,
            extra_body=extra_body,
            phase=phase,
            iteration=iteration,
            traced_messages=traced_messages,
        )
        return response.choices[0].message.content or ""

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def _build_user_content(
        self,
        prompt: str,
        image_paths: list[str] | None = None,
    ) -> str | list[dict[str, Any]]:
        if not image_paths:
            return prompt

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._image_path_to_data_url(path)},
                }
            )
        return content

    @staticmethod
    def _image_path_to_data_url(image_path: str) -> str:
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Image file not found: {path}")

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type is None or not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported image type: {path}")

        encoded_bytes = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded_bytes}"
