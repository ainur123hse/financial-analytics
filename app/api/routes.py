from __future__ import annotations

import asyncio
import shutil
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from celery.result import AsyncResult
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status

from app.api.celery_app import celery_app
from app.api.redis_store import (
    active_task_count,
    add_active_task,
    is_task_registered,
    mark_task_registered,
    remove_active_task,
    remove_task_registration,
    release_stems,
    reserve_stems,
)
from app.api.schemas import (
    ConversionAcceptedFile,
    ConversionCreateResponse,
    ConversionItemResult,
    ConversionStatusResponse,
    QARequest,
    QAResponse,
)
from app.api.tasks import convert_pdf_batch
from app.config import settings
from app.documents_preprocessing.make_markdown import MARKDOWNS_DIR
from app.main_agent.run import answer_to_question

router = APIRouter(prefix="/api/v1", tags=["financial-analytics"])

UPLOADED_PDFS_DIR = Path(settings.UPLOADED_PDFS_DIR)
UPLOADED_PDFS_DIR.mkdir(parents=True, exist_ok=True)
MARKDOWNS_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_uploaded_filename(upload: UploadFile) -> str:
    raw_name = upload.filename or ""
    return Path(raw_name).name


def _validate_pdf_batch(files: list[UploadFile]) -> list[dict[str, object]]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one PDF file must be provided.",
        )

    normalized_files: list[dict[str, object]] = []
    invalid_files: list[str] = []

    for upload in files:
        filename = _normalize_uploaded_filename(upload)
        if not filename:
            invalid_files.append("<empty filename>")
            continue

        path = Path(filename)
        if path.suffix.lower() != ".pdf":
            invalid_files.append(filename)
            continue

        stem = path.stem.strip()
        if not stem:
            invalid_files.append(filename)
            continue

        normalized_files.append(
            {
                "upload": upload,
                "filename": filename,
                "stem": stem,
            }
        )

    if invalid_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "All files must be valid PDF files.",
                "invalid_files": invalid_files,
            },
        )

    stems = [entry["stem"] for entry in normalized_files]
    stem_counts = Counter(stems)
    duplicated_stems = sorted(stem for stem, count in stem_counts.items() if count > 1)
    if duplicated_stems:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Batch contains duplicated PDF stems.",
                "conflicting_stems": duplicated_stems,
            },
        )

    return normalized_files


def _find_existing_output_conflicts(stems: list[str]) -> list[str]:
    conflicts: list[str] = []
    for stem in stems:
        markdown_path = MARKDOWNS_DIR / f"{stem}.md"
        images_dir = MARKDOWNS_DIR / f"{stem}_images"
        if markdown_path.exists() or images_dir.exists():
            conflicts.append(stem)
    return sorted(conflicts)


async def _wait_for_no_active_conversions() -> bool:
    timeout_sec = float(settings.QA_WAIT_TIMEOUT_SECONDS)
    poll_interval_sec = float(settings.QA_WAIT_POLL_INTERVAL_SECONDS)

    deadline = time.monotonic() + timeout_sec
    while True:
        if active_task_count() == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_interval_sec)


@router.post(
    "/conversions",
    response_model=ConversionCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_conversion_task(
    request: Request,
    files: list[UploadFile] = File(...),
) -> ConversionCreateResponse:
    normalized_files = _validate_pdf_batch(files=files)
    stems = [entry["stem"] for entry in normalized_files]

    existing_conflicts = _find_existing_output_conflicts(stems=stems)
    if existing_conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Output paths already exist for these stems.",
                "conflicting_stems": existing_conflicts,
            },
        )

    task_id = str(uuid4())
    lock_owner = task_id

    conflict_stem = reserve_stems(stems=stems, owner=lock_owner)
    if conflict_stem is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "A conversion task is already reserving one of these stems.",
                "conflicting_stem": conflict_stem,
            },
        )

    upload_batch_dir = UPLOADED_PDFS_DIR / task_id
    task_files_payload: list[dict[str, str]] = []

    try:
        upload_batch_dir.mkdir(parents=True, exist_ok=False)

        for entry in normalized_files:
            upload = entry["upload"]
            filename = entry["filename"]
            stem = entry["stem"]

            destination = upload_batch_dir / filename
            upload.file.seek(0)
            with destination.open("wb") as output_file:
                shutil.copyfileobj(upload.file, output_file)
            await upload.close()

            task_files_payload.append(
                {
                    "original_filename": str(filename),
                    "stem": str(stem),
                    "source_path": str(destination),
                }
            )

        add_active_task(task_id=task_id)
        mark_task_registered(task_id=task_id)
        convert_pdf_batch.apply_async(
            kwargs={
                "task_id": task_id,
                "files": task_files_payload,
                "stems": stems,
                "lock_owner": lock_owner,
            },
            task_id=task_id,
        )
    except Exception as exc:
        try:
            release_stems(stems=stems, owner=lock_owner)
        finally:
            remove_active_task(task_id=task_id)
            remove_task_registration(task_id=task_id)
            if upload_batch_dir.exists():
                shutil.rmtree(upload_batch_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue conversion task: {exc}",
        ) from exc

    accepted_files = [
        ConversionAcceptedFile(filename=str(entry["filename"]), stem=str(entry["stem"]))
        for entry in normalized_files
    ]

    return ConversionCreateResponse(
        task_id=task_id,
        status_url=str(request.url_for("get_conversion_status", task_id=task_id)),
        files=accepted_files,
    )


@router.get(
    "/conversions/{task_id}",
    response_model=ConversionStatusResponse,
    name="get_conversion_status",
)
async def get_conversion_status(task_id: str) -> ConversionStatusResponse:
    if not is_task_registered(task_id=task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not found.",
        )

    result = AsyncResult(task_id, app=celery_app)
    state = result.state

    if state in {"PENDING", "RECEIVED", "RETRY"}:
        return ConversionStatusResponse(task_id=task_id, status="queued")

    if state == "STARTED":
        return ConversionStatusResponse(task_id=task_id, status="running")

    if state == "FAILURE":
        return ConversionStatusResponse(
            task_id=task_id,
            status="failed",
            error=str(result.result),
        )

    if state == "SUCCESS" and isinstance(result.result, dict):
        payload = result.result
        payload_status = str(payload.get("status") or "completed")
        items_raw = payload.get("items") or []
        items = [ConversionItemResult.model_validate(item) for item in items_raw]
        return ConversionStatusResponse(
            task_id=task_id,
            status=payload_status,
            items=items,
            error=payload.get("error"),
        )

    return ConversionStatusResponse(task_id=task_id, status="queued")


@router.post("/qa", response_model=QAResponse)
async def answer_question(request: QARequest) -> QAResponse:
    ready = await _wait_for_no_active_conversions()
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Timed out waiting for active conversion tasks to finish. "
                "Try again later."
            ),
        )

    markdowns_dir = Path(settings.MARKDOWNS_DIR)
    markdowns_dir.mkdir(parents=True, exist_ok=True)

    answer = await answer_to_question(
        user_question=request.question,
        md_dir_path=markdowns_dir,
    )
    return QAResponse(answer=answer)
