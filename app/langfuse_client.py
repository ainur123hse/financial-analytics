from __future__ import annotations

import logging
from functools import lru_cache
from types import TracebackType
from typing import Any, Literal

from langfuse import Langfuse, propagate_attributes

from app.config import settings

logger = logging.getLogger(__name__)

ObservationType = Literal[
    "span",
    "generation",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "embedding",
    "guardrail",
]


class _SafeContextManager:
    def __init__(self, context_manager: Any | None, context_name: str) -> None:
        self._context_manager = context_manager
        self._context_name = context_name
        self._active_context_manager: Any | None = None

    def __enter__(self) -> Any | None:
        if self._context_manager is None:
            return None

        self._active_context_manager = self._context_manager
        try:
            return self._active_context_manager.__enter__()
        except Exception:
            logger.exception("Failed to enter Langfuse context `%s`.", self._context_name)
            self._active_context_manager = None
            return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._active_context_manager is None:
            return False

        try:
            return bool(self._active_context_manager.__exit__(exc_type, exc, tb))
        except Exception:
            logger.exception("Failed to exit Langfuse context `%s`.", self._context_name)
            return False


def is_langfuse_enabled() -> bool:
    return bool(
        settings.LANGFUSE_TRACING_ENABLED
        and settings.LANGFUSE_PUBLIC_KEY
        and settings.LANGFUSE_SECRET_KEY
    )


@lru_cache(maxsize=1)
def get_langfuse_client() -> Langfuse | None:
    if not is_langfuse_enabled():
        return None

    try:
        return Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            base_url=settings.LANGFUSE_BASE_URL,
            tracing_enabled=settings.LANGFUSE_TRACING_ENABLED,
            environment=settings.LANGFUSE_TRACING_ENVIRONMENT,
            release=settings.LANGFUSE_RELEASE,
        )
    except Exception:
        logger.exception("Failed to initialize Langfuse client.")
        return None


def start_observation_context(
    *,
    name: str,
    as_type: ObservationType = "span",
    input: Any | None = None,
    output: Any | None = None,
    metadata: Any | None = None,
) -> _SafeContextManager:
    langfuse_client = get_langfuse_client()
    if langfuse_client is None:
        return _SafeContextManager(context_manager=None, context_name=name)

    try:
        context_manager = langfuse_client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=input,
            output=output,
            metadata=metadata,
        )
        return _SafeContextManager(context_manager=context_manager, context_name=name)
    except Exception:
        logger.exception("Failed to start Langfuse observation `%s`.", name)
        return _SafeContextManager(context_manager=None, context_name=name)


def trace_attributes_context(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, str] | None = None,
    tags: list[str] | None = None,
    trace_name: str | None = None,
) -> _SafeContextManager:
    if get_langfuse_client() is None:
        return _SafeContextManager(context_manager=None, context_name="trace_attributes")

    try:
        context_manager = propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
            tags=tags,
            trace_name=trace_name,
        )
        return _SafeContextManager(
            context_manager=context_manager,
            context_name="trace_attributes",
        )
    except Exception:
        logger.exception("Failed to propagate Langfuse trace attributes.")
        return _SafeContextManager(context_manager=None, context_name="trace_attributes")


def safe_update_observation(observation: Any | None, **kwargs: Any) -> None:
    if observation is None:
        return

    try:
        observation.update(**kwargs)
    except Exception:
        logger.exception("Failed to update Langfuse observation.")


def flush_langfuse() -> None:
    langfuse_client = get_langfuse_client()
    if langfuse_client is None:
        return

    try:
        langfuse_client.flush()
    except Exception:
        logger.exception("Failed to flush Langfuse traces.")
