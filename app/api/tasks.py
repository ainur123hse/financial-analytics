from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.api.celery_app import celery_app
from app.api.redis_store import release_thread_stems
from app.codex_runner.run import GenerationCodexRunResult, run_generation_codex_in_docker
from app.config import settings
from app.db import session_scope
from app.langfuse_client import (
    emit_codex_step_observations,
    flush_langfuse,
    safe_update_observation,
    start_observation_context,
    trace_attributes_context,
)
from app.models import (
    ACTIVE_JOB_STATUSES,
    JOB_KIND_CONVERSION,
    JOB_KIND_GENERATION,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    Document,
    Job,
    Message,
    Thread,
    utcnow,
)
from app.storage import (
    delete_document_files,
    thread_documents_dir,
)

logger = logging.getLogger(__name__)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_generation_langfuse_output(run_result: GenerationCodexRunResult) -> dict[str, Any]:
    step_counts = Counter(step.step_type for step in run_result.steps)
    output: dict[str, Any] = {
        "analysis_markdown": run_result.analysis_markdown,
        "runner_container_name": run_result.runner_container_name,
        "codex_session_id": run_result.codex_session_id,
        "codex_model": run_result.codex_model,
        "step_count": len(run_result.steps),
        "step_counts": dict(step_counts),
    }
    return output


def _rollback_batch_outputs(thread_id: str, kind: str, stems: list[str]) -> None:
    for stem in stems:
        delete_document_files(
            thread_id=thread_id,
            kind=kind,
            markdown_filename=f"{stem}.md",
            images_dirname=f"{stem}_images",
            artifacts_dirname=f"{stem}_artifacts",
        )


def _materialize_conversion_source(
    file_meta: dict[str, str],
    *,
    temp_dir: Path,
) -> Path:
    content_b64 = file_meta.get("content_b64")
    filename = file_meta["original_filename"]
    if content_b64 is not None:
        destination = temp_dir / filename
        file_bytes = base64.b64decode(content_b64.encode("ascii"))
        destination.write_bytes(file_bytes)
        logger.info(
            "Materialized conversion source filename=%s destination=%s size=%s sha256=%s",
            filename,
            destination,
            len(file_bytes),
            _sha256_hex(file_bytes),
        )
        return destination

    source_path_raw = file_meta.get("source_path")
    if not source_path_raw:
        raise FileNotFoundError(f"Source file payload is missing for `{filename}`.")

    source_path = Path(source_path_raw)
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    return source_path


def _set_job_running(task_id: str) -> None:
    with session_scope() as session:
        job = session.get(Job, task_id)
        if job is None:
            raise RuntimeError(f"Job '{task_id}' was not found.")
        job.status = JOB_STATUS_RUNNING
        job.error = None
        job.result_payload = None
        job.completed_at = None


def _finish_job(
    *,
    task_id: str,
    status: str,
    error: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> None:
    with session_scope() as session:
        job = session.get(Job, task_id)
        if job is None:
            raise RuntimeError(f"Job '{task_id}' was not found.")
        job.status = status
        job.error = error
        job.result_payload = result_payload
        job.completed_at = utcnow()


def _wait_for_no_active_conversions(thread_id: str) -> None:
    timeout_sec = float(settings.QA_WAIT_TIMEOUT_SECONDS)
    poll_interval_sec = float(settings.QA_WAIT_POLL_INTERVAL_SECONDS)

    deadline = time.monotonic() + timeout_sec
    while True:
        with session_scope() as session:
            active_conversion = session.execute(
                select(Job.id).where(
                    Job.thread_id == thread_id,
                    Job.kind == JOB_KIND_CONVERSION,
                    Job.status.in_(ACTIVE_JOB_STATUSES),
                )
            ).scalar_one_or_none()
        if active_conversion is None:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for active conversion tasks in this thread to finish. Try again later."
            )
        time.sleep(poll_interval_sec)


@celery_app.task(name="conversions.convert_document_batch", bind=True)
def convert_document_batch(
    self,
    task_id: str,
    thread_id: str,
    user_id: str,
    kind: str,
    files: list[dict[str, str]],
    stems: list[str],
    lock_owner: str,
) -> dict[str, object]:
    from app.documents_preprocessing.make_markdown import make_markdown

    _set_job_running(task_id)
    thread_dir = thread_documents_dir(thread_id, kind)
    thread_dir.mkdir(parents=True, exist_ok=True)

    public_items: list[dict[str, str | None]] = []
    converted_outputs: list[dict[str, str]] = []
    processed_stems: set[str] = set()

    try:
        with tempfile.TemporaryDirectory(prefix=f"fa-conversion-{task_id}-") as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            for file_meta in files:
                item_kind = file_meta["kind"]
                filename = file_meta["original_filename"]
                stem = file_meta["stem"]
                source_path = _materialize_conversion_source(file_meta, temp_dir=temp_dir)
                logger.info(
                    "Starting conversion task_id=%s thread_id=%s filename=%s stem=%s source_path=%s",
                    task_id,
                    thread_id,
                    filename,
                    stem,
                    source_path,
                )

                try:
                    markdown = asyncio.run(
                        make_markdown(
                            source_path=source_path,
                            output_dir=thread_dir,
                        )
                    )
                    converted_outputs.append(
                        {
                            "kind": item_kind,
                            "original_filename": filename,
                            "stem": stem,
                            "markdown_filename": markdown.markdown_path.name,
                            "images_dirname": markdown.images_dir_path.name,
                            "artifacts_dirname": f"{stem}_artifacts",
                        }
                    )
                    public_items.append(
                        {
                            "kind": item_kind,
                            "filename": filename,
                            "stem": stem,
                            "document_id": None,
                            "error": None,
                        }
                    )
                    processed_stems.add(stem)
                except Exception as exc:
                    logger.exception(
                        "Conversion failed task_id=%s thread_id=%s filename=%s stem=%s source_path=%s",
                        task_id,
                        thread_id,
                        filename,
                        stem,
                        source_path,
                    )
                    failure_message = f"{filename}: {exc}"
                    public_items.append(
                        {
                            "kind": item_kind,
                            "filename": filename,
                            "stem": stem,
                            "document_id": None,
                            "error": failure_message,
                        }
                    )
                    _rollback_batch_outputs(thread_id=thread_id, kind=kind, stems=stems)

                    for item in public_items:
                        if item["stem"] in processed_stems:
                            item["error"] = "Rolled back because another file in this batch failed."

                    seen_stems = {item["stem"] for item in public_items}
                    for skipped_meta in files:
                        skipped_stem = skipped_meta["stem"]
                        if skipped_stem in seen_stems:
                            continue
                        public_items.append(
                            {
                                "kind": kind,
                                "filename": skipped_meta["original_filename"],
                                "stem": skipped_stem,
                                "document_id": None,
                                "error": "Skipped because batch processing already failed.",
                            }
                        )

                    result_payload = {
                        "task_id": task_id,
                        "items": public_items,
                    }
                    _finish_job(
                        task_id=task_id,
                        status=JOB_STATUS_FAILED,
                        error=failure_message,
                        result_payload=result_payload,
                    )
                    return {
                        "task_id": task_id,
                        "status": JOB_STATUS_FAILED,
                        "items": public_items,
                        "error": failure_message,
                    }

        with session_scope() as session:
            thread = session.get(Thread, thread_id)
            if thread is None:
                raise RuntimeError(f"Thread '{thread_id}' was not found.")

            for output in converted_outputs:
                document = Document(
                    id=str(uuid4()),
                    thread_id=thread_id,
                    kind=output["kind"],
                    original_filename=output["original_filename"],
                    stem=output["stem"],
                    markdown_filename=output["markdown_filename"],
                    images_dirname=output["images_dirname"],
                    artifacts_dirname=output["artifacts_dirname"],
                )
                session.add(document)
                session.flush()

                for item in public_items:
                    if item["stem"] == output["stem"] and item["filename"] == output["original_filename"]:
                        item["document_id"] = document.id
                        break

            thread.updated_at = utcnow()

        result_payload = {
            "task_id": task_id,
            "items": public_items,
        }
        _finish_job(
            task_id=task_id,
            status=JOB_STATUS_COMPLETED,
            error=None,
            result_payload=result_payload,
        )
        return {
            "task_id": task_id,
            "status": JOB_STATUS_COMPLETED,
            "items": public_items,
            "error": None,
        }
    except Exception as exc:
        _rollback_batch_outputs(thread_id=thread_id, kind=kind, stems=stems)
        result_payload = {
            "task_id": task_id,
            "items": public_items,
        }
        _finish_job(
            task_id=task_id,
            status=JOB_STATUS_FAILED,
            error=str(exc),
            result_payload=result_payload,
        )
        return {
            "task_id": task_id,
            "status": JOB_STATUS_FAILED,
            "items": public_items,
            "error": str(exc),
        }
    finally:
        release_thread_stems(thread_id=thread_id, kind=kind, stems=stems, owner=lock_owner)


@celery_app.task(name="generation.generate_analytics_by_period", bind=True)
def generate_analytics_by_period(
    self,
    task_id: str,
    thread_id: str,
    user_id: str,
    period_description: str,
    user_message_id: str,
) -> dict[str, object]:
    root_observation = None
    assistant_message_id: str | None = None
    try:
        _set_job_running(task_id)

        with trace_attributes_context(
            user_id=user_id,
            session_id=task_id,
            metadata={
                "component": "generation_codex_runner",
                "flow": "generation",
                "task_id": task_id,
                "thread_id": thread_id,
            },
            tags=["generation", "codex_runner"],
            trace_name="financial-analytics.generation.codex_runner",
        ):
            with start_observation_context(
                name="generation.generate_analytics_by_period",
                as_type="agent",
                input={
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "period_description": period_description,
                },
                metadata={"runner_image": settings.QA_RUNNER_IMAGE},
            ) as root_observation:
                try:
                    _wait_for_no_active_conversions(thread_id)
                    run_result = run_generation_codex_in_docker(
                        task_id=task_id,
                        period_description=period_description,
                        source_subpath=f"threads/{thread_id}",
                    )

                    safe_update_observation(
                        root_observation,
                        output=_build_generation_langfuse_output(run_result),
                    )
                    flush_langfuse()

                    emit_codex_step_observations(
                        root_observation=root_observation,
                        task_id=task_id,
                        steps=run_result.steps,
                        model_name=run_result.codex_model,
                    )

                    assistant_message_id = str(uuid4())
                    with session_scope() as session:
                        thread = session.get(Thread, thread_id)
                        if thread is None:
                            raise RuntimeError(f"Thread '{thread_id}' was not found.")

                        session.add(
                            Message(
                                id=assistant_message_id,
                                thread_id=thread_id,
                                role="assistant",
                                content=run_result.analysis_markdown,
                            )
                        )
                        thread.updated_at = utcnow()

                    result_payload = {
                        "task_id": task_id,
                        "analysis_markdown": run_result.analysis_markdown,
                        "assistant_message_id": assistant_message_id,
                    }
                    _finish_job(
                        task_id=task_id,
                        status=JOB_STATUS_COMPLETED,
                        error=None,
                        result_payload=result_payload,
                    )
                    return {
                        "task_id": task_id,
                        "status": JOB_STATUS_COMPLETED,
                        "analysis_markdown": run_result.analysis_markdown,
                        "assistant_message_id": assistant_message_id,
                        "error": None,
                    }
                except Exception as exc:
                    safe_update_observation(
                        root_observation,
                        level="ERROR",
                        status_message=str(exc),
                    )
                    raise
    except Exception as exc:
        _finish_job(
            task_id=task_id,
            status=JOB_STATUS_FAILED,
            error=str(exc),
            result_payload={
                "task_id": task_id,
                "analysis_markdown": None,
                "assistant_message_id": assistant_message_id,
            },
        )
        return {
            "task_id": task_id,
            "status": JOB_STATUS_FAILED,
            "analysis_markdown": None,
            "assistant_message_id": assistant_message_id,
            "error": str(exc),
        }
    finally:
        flush_langfuse()
