from __future__ import annotations

import importlib
import mimetypes
import warnings
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.config import settings


class _NoopObservation:
    def update(self, **_: Any) -> None:
        return None


class _ObservabilityClient:
    def __init__(self) -> None:
        self._client: Any | None = None
        self._langfuse_module: Any | None = None
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        if (
            not settings.LANGFUSE_TRACING_ENABLED
            or not settings.LANGFUSE_PUBLIC_KEY
            or not settings.LANGFUSE_SECRET_KEY
        ):
            return

        try:
            langfuse_module = importlib.import_module("langfuse")
        except ImportError:
            warnings.warn(
                "Langfuse tracing is configured but the 'langfuse' package is not installed.",
                RuntimeWarning,
                stacklevel=2,
            )
            return

        try:
            self._client = langfuse_module.Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                base_url=settings.LANGFUSE_BASE_URL,
                tracing_enabled=settings.LANGFUSE_TRACING_ENABLED,
                environment=settings.LANGFUSE_TRACING_ENVIRONMENT,
                release=settings.LANGFUSE_RELEASE,
            )
            self._langfuse_module = langfuse_module
        except Exception as exc:  # pragma: no cover - depends on third-party SDK internals
            warnings.warn(
                f"Failed to initialize Langfuse tracing: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._client = None
            self._langfuse_module = None

    def get_client(self) -> Any | None:
        self._initialize()
        return self._client

    def get_module(self) -> Any | None:
        self._initialize()
        return self._langfuse_module

    def reset(self) -> None:
        self._client = None
        self._langfuse_module = None
        self._initialized = False


_client = _ObservabilityClient()


def reset_observability() -> None:
    _client.reset()


@contextmanager
def start_observation(**kwargs: Any) -> Iterator[Any]:
    client = _client.get_client()
    if client is None:
        yield _NoopObservation()
        return

    with client.start_as_current_observation(**kwargs) as observation:
        yield observation


def propagate_attributes(**kwargs: Any) -> Any:
    langfuse_module = _client.get_module()
    if langfuse_module is None:
        return nullcontext()
    return langfuse_module.propagate_attributes(**kwargs)


def update_observation(observation: Any, **kwargs: Any) -> None:
    try:
        observation.update(**kwargs)
    except Exception as exc:  # pragma: no cover - depends on third-party SDK internals
        warnings.warn(
            f"Failed to update Langfuse observation: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


def shutdown_observability() -> None:
    client = _client.get_client()
    if client is None:
        return

    try:
        client.flush()
    except Exception as exc:  # pragma: no cover - depends on third-party SDK internals
        warnings.warn(
            f"Failed to flush Langfuse tracing data: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    try:
        client.shutdown()
    except Exception as exc:  # pragma: no cover - depends on third-party SDK internals
        warnings.warn(
            f"Failed to shut down Langfuse tracing: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
    finally:
        _client.reset()


def serialize_for_langfuse(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, Mapping):
        return {
            str(key): serialize_for_langfuse(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [serialize_for_langfuse(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return serialize_for_langfuse(model_dump(mode="json"))

    if hasattr(value, "__dict__"):
        return serialize_for_langfuse(vars(value))

    return str(value)


def build_traced_user_content(
    prompt: str,
    image_paths: list[str] | None = None,
) -> str | list[dict[str, Any]]:
    if not image_paths:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        display_path = _display_image_path(path)
        mime_type, _ = mimetypes.guess_type(display_path)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"<image-data omitted: {display_path}>"},
                "image_path": display_path,
                "mime_type": mime_type or "unknown",
            }
        )
    return content


def sanitize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        sanitized_message = dict(message)
        sanitized_message["content"] = _sanitize_content(message.get("content"))
        sanitized.append(serialize_for_langfuse(sanitized_message))
    return sanitized


def serialize_openai_response(response: Any) -> Any:
    return serialize_for_langfuse(response)


def extract_usage_details(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    usage_dict = serialize_for_langfuse(usage)
    if not isinstance(usage_dict, dict):
        return None

    extracted: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage_dict.get(field)
        if isinstance(value, int):
            extracted[field] = value

    return extracted or None


def _sanitize_content(content: Any) -> Any:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return serialize_for_langfuse(content)

    sanitized_parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            sanitized_parts.append({"value": serialize_for_langfuse(part)})
            continue

        sanitized_part = dict(part)
        if sanitized_part.get("type") != "image_url":
            sanitized_parts.append(serialize_for_langfuse(sanitized_part))
            continue

        image_url = sanitized_part.get("image_url")
        if not isinstance(image_url, Mapping):
            sanitized_parts.append(serialize_for_langfuse(sanitized_part))
            continue

        url = image_url.get("url")
        if isinstance(url, str) and url.startswith("data:"):
            sanitized_part["image_url"] = {"url": f"<image-data omitted: {_extract_mime_type(url)}>"}
            sanitized_part["mime_type"] = _extract_mime_type(url)

        sanitized_parts.append(serialize_for_langfuse(sanitized_part))

    return sanitized_parts


def _display_image_path(path: str) -> str:
    candidate = Path(path)
    for anchor in ("artifacts", "notes"):
        if anchor in candidate.parts:
            anchor_index = candidate.parts.index(anchor)
            return str(Path(*candidate.parts[anchor_index:]))
    return candidate.name or str(candidate)


def _extract_mime_type(data_url: str) -> str:
    header = data_url.split(",", 1)[0]
    return header.removeprefix("data:").split(";", 1)[0] or "unknown"
