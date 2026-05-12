from __future__ import annotations

import base64
import hashlib
import logging
from collections import Counter
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.redis_store import release_thread_stems, reserve_thread_stems
from app.api.schemas import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthUserResponse,
    ConversionAcceptedFile,
    ConversionCreateResponse,
    ConversionItemResult,
    ConversionStatusResponse,
    DocumentResponse,
    GenerationCreateResponse,
    GenerationRequest,
    GenerationStatusResponse,
    MessageResponse,
    ThreadCreateRequest,
    ThreadResponse,
    ThreadUpdateRequest,
)
from app.api.tasks import convert_document_batch, generate_analytics_by_period
from app.auth import (
    clear_session_cookie,
    create_session_token,
    hash_password,
    normalize_email,
    set_session_cookie,
    verify_password,
)
from app.db import get_db_session
from app.documents_preprocessing.docling_converter import (
    is_supported_source_path,
    supported_source_extensions_label,
)
from app.models import (
    ACTIVE_JOB_STATUSES,
    DOCUMENT_KIND_ANALYTICS,
    DOCUMENT_KIND_SOURCES,
    DOCUMENT_KINDS,
    DEFAULT_THREAD_TITLE,
    JOB_KIND_CONVERSION,
    JOB_KIND_GENERATION,
    JOB_STATUS_QUEUED,
    Document,
    Job,
    Message,
    Thread,
    User,
    utcnow,
)
from app.storage import (
    delete_document_files,
    delete_thread_storage,
    ensure_thread_dirs,
    thread_artifacts_dir,
    thread_images_dir,
    thread_markdown_path,
)

router = APIRouter(prefix="/api/v1", tags=["financial-analytics"])
logger = logging.getLogger(__name__)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_uploaded_filename(upload: UploadFile) -> str:
    raw_name = upload.filename or ""
    return Path(raw_name).name


def _normalize_thread_title(raw_title: str | None) -> str:
    title = (raw_title or "").strip()
    return title or DEFAULT_THREAD_TITLE


def _validate_email_address(email: str) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A valid email address is required.",
        )
    return normalized


def _validate_document_kind(raw_kind: str) -> str:
    kind = raw_kind.strip().lower()
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Unsupported document_kind.",
                "allowed_values": list(DOCUMENT_KINDS),
            },
        )
    return kind


def _validate_source_batch(files: list[UploadFile], *, document_kind: str) -> list[dict[str, object]]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one supported document must be provided.",
        )

    normalized_files: list[dict[str, object]] = []
    invalid_files: list[str] = []

    for upload in files:
        filename = _normalize_uploaded_filename(upload)
        if not filename:
            invalid_files.append("<empty filename>")
            continue

        path = Path(filename)
        if not is_supported_source_path(path):
            invalid_files.append(filename)
            continue

        stem = path.stem.strip()
        if not stem:
            invalid_files.append(filename)
            continue

        normalized_files.append(
            {
                "upload": upload,
                "kind": document_kind,
                "filename": filename,
                "stem": stem,
            }
        )

    if invalid_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"All files must use one of: {supported_source_extensions_label()}.",
                "invalid_files": invalid_files,
            },
        )

    stems = [str(entry["stem"]) for entry in normalized_files]
    stem_counts = Counter(stems)
    duplicated_stems = sorted(stem for stem, count in stem_counts.items() if count > 1)
    if duplicated_stems:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Batch contains duplicated document stems.",
                "conflicting_stems": duplicated_stems,
            },
        )

    return normalized_files


def _get_owned_thread(
    session: Session,
    *,
    user_id: str,
    thread_id: str,
    for_update: bool = False,
) -> Thread | None:
    stmt = select(Thread).where(Thread.id == thread_id, Thread.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _require_owned_thread(
    session: Session,
    *,
    user_id: str,
    thread_id: str,
    for_update: bool = False,
) -> Thread:
    thread = _get_owned_thread(
        session,
        user_id=user_id,
        thread_id=thread_id,
        for_update=for_update,
    )
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thread '{thread_id}' was not found.",
        )
    return thread


def _has_active_job(session: Session, *, thread_id: str, kind: str | None = None) -> bool:
    stmt = select(Job.id).where(
        Job.thread_id == thread_id,
        Job.status.in_(ACTIVE_JOB_STATUSES),
    )
    if kind is not None:
        stmt = stmt.where(Job.kind == kind)
    stmt = stmt.limit(1)
    return session.execute(stmt).scalar_one_or_none() is not None


def _find_existing_output_conflicts(
    session: Session,
    *,
    thread_id: str,
    document_kind: str,
    stems: list[str],
) -> list[str]:
    existing_stems = set(
        session.execute(
            select(Document.stem).where(
                Document.thread_id == thread_id,
                Document.kind == document_kind,
                Document.stem.in_(stems),
            )
        ).scalars()
    )

    for stem in stems:
        markdown_path = thread_markdown_path(thread_id, document_kind, stem)
        images_dir = thread_images_dir(thread_id, document_kind, stem)
        artifacts_dir = thread_artifacts_dir(thread_id, document_kind, stem)
        if markdown_path.exists() or images_dir.exists() or artifacts_dir.exists():
            existing_stems.add(stem)

    return sorted(existing_stems)


def _get_owned_job(
    session: Session,
    *,
    user_id: str,
    thread_id: str,
    task_id: str,
    kind: str,
) -> Job:
    job = session.execute(
        select(Job).where(
            Job.id == task_id,
            Job.user_id == user_id,
            Job.thread_id == thread_id,
            Job.kind == kind,
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not found.",
        )
    return job


def _build_conversion_status_response(job: Job) -> ConversionStatusResponse:
    payload = job.result_payload or {}
    items = [ConversionItemResult.model_validate(item) for item in payload.get("items", [])]
    return ConversionStatusResponse(
        task_id=job.id,
        status=job.status,
        items=items,
        error=job.error,
    )


def _build_generation_status_response(job: Job) -> GenerationStatusResponse:
    payload = job.result_payload or {}
    return GenerationStatusResponse(
        task_id=job.id,
        status=job.status,
        analysis_markdown=payload.get("analysis_markdown"),
        error=job.error,
        assistant_message_id=payload.get("assistant_message_id"),
    )


async def _close_uploads(normalized_files: list[dict[str, object]]) -> None:
    for entry in normalized_files:
        upload = entry["upload"]
        if isinstance(upload, UploadFile):
            await upload.close()


@router.post("/auth/register", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    auth_request: AuthRegisterRequest,
    response: Response,
    session: Session = Depends(get_db_session),
) -> AuthUserResponse:
    email = _validate_email_address(auth_request.email)

    if session.execute(select(User.id).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(auth_request.password),
    )
    session.add(user)
    session.commit()

    token = create_session_token(user_id=user.id, email=user.email)
    set_session_cookie(response, token)
    return AuthUserResponse.model_validate(user, from_attributes=True)


@router.post("/auth/login", response_model=AuthUserResponse)
async def login(
    auth_request: AuthLoginRequest,
    response: Response,
    session: Session = Depends(get_db_session),
) -> AuthUserResponse:
    email = _validate_email_address(auth_request.email)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not verify_password(auth_request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    token = create_session_token(user_id=user.id, email=user.email)
    set_session_cookie(response, token)
    return AuthUserResponse.model_validate(user, from_attributes=True)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> Response:
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/auth/me", response_model=AuthUserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> AuthUserResponse:
    return AuthUserResponse.model_validate(current_user, from_attributes=True)


@router.get("/threads", response_model=list[ThreadResponse])
async def list_threads(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> list[ThreadResponse]:
    threads = session.execute(
        select(Thread)
        .where(Thread.user_id == current_user.id)
        .order_by(Thread.updated_at.desc(), Thread.created_at.desc())
    ).scalars()
    return [ThreadResponse.model_validate(thread, from_attributes=True) for thread in threads]


@router.post("/threads", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED)
async def create_thread(
    thread_request: ThreadCreateRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> ThreadResponse:
    thread = Thread(
        id=str(uuid4()),
        user_id=current_user.id,
        title=_normalize_thread_title(thread_request.title),
    )
    session.add(thread)
    session.commit()
    ensure_thread_dirs(thread.id)
    return ThreadResponse.model_validate(thread, from_attributes=True)


@router.get("/threads/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> ThreadResponse:
    thread = _require_owned_thread(session, user_id=current_user.id, thread_id=thread_id)
    return ThreadResponse.model_validate(thread, from_attributes=True)


@router.patch("/threads/{thread_id}", response_model=ThreadResponse)
async def update_thread(
    thread_id: str,
    thread_request: ThreadUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> ThreadResponse:
    thread = _require_owned_thread(
        session,
        user_id=current_user.id,
        thread_id=thread_id,
        for_update=True,
    )
    thread.title = thread_request.title.strip()
    if not thread.title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Thread title must not be empty.",
        )
    session.commit()
    session.refresh(thread)
    return ThreadResponse.model_validate(thread, from_attributes=True)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> Response:
    thread = _require_owned_thread(
        session,
        user_id=current_user.id,
        thread_id=thread_id,
        for_update=True,
    )
    if _has_active_job(session, thread_id=thread.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a thread with active jobs.",
        )

    session.delete(thread)
    session.commit()
    delete_thread_storage(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/threads/{thread_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> list[DocumentResponse]:
    _require_owned_thread(session, user_id=current_user.id, thread_id=thread_id)
    documents = session.execute(
        select(Document)
        .where(Document.thread_id == thread_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    ).scalars()
    return [DocumentResponse.model_validate(document, from_attributes=True) for document in documents]


@router.delete(
    "/threads/{thread_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    thread_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> Response:
    thread = _require_owned_thread(
        session,
        user_id=current_user.id,
        thread_id=thread_id,
        for_update=True,
    )
    if _has_active_job(session, thread_id=thread_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete documents while jobs are active for this thread.",
        )

    document = session.execute(
        select(Document).where(Document.id == document_id, Document.thread_id == thread_id)
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' was not found.",
        )

    markdown_filename = document.markdown_filename
    images_dirname = document.images_dirname
    artifacts_dirname = document.artifacts_dirname
    thread.updated_at = utcnow()
    session.delete(document)
    session.commit()

    delete_document_files(
        thread_id=thread_id,
        kind=document.kind,
        markdown_filename=markdown_filename,
        images_dirname=images_dirname,
        artifacts_dirname=artifacts_dirname,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/threads/{thread_id}/conversions",
    response_model=ConversionCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_conversion_task(
    thread_id: str,
    request: Request,
    document_kind: str = Form(...),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> ConversionCreateResponse:
    document_kind = _validate_document_kind(document_kind)
    normalized_files = _validate_source_batch(files=files, document_kind=document_kind)
    stems = [str(entry["stem"]) for entry in normalized_files]
    task_id = str(uuid4())
    lock_owner = task_id

    try:
        _require_owned_thread(
            session,
            user_id=current_user.id,
            thread_id=thread_id,
            for_update=True,
        )
        if _has_active_job(session, thread_id=thread_id, kind=JOB_KIND_CONVERSION):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A conversion task is already active for this thread.",
            )

        existing_conflicts = _find_existing_output_conflicts(
            session,
            thread_id=thread_id,
            document_kind=document_kind,
            stems=stems,
        )
        if existing_conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Output paths already exist for these stems in this thread.",
                    "conflicting_stems": existing_conflicts,
                },
            )

        session.add(
            Job(
                id=task_id,
                user_id=current_user.id,
                thread_id=thread_id,
                kind=JOB_KIND_CONVERSION,
                status=JOB_STATUS_QUEUED,
            )
        )
        session.commit()

        conflict_stem = reserve_thread_stems(
            thread_id=thread_id,
            kind=document_kind,
            stems=stems,
            owner=lock_owner,
        )
        if conflict_stem is not None:
            session.execute(delete(Job).where(Job.id == task_id))
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "A conversion task is already reserving one of these stems.",
                    "conflicting_stem": conflict_stem,
                },
            )

        task_files_payload: list[dict[str, str]] = []

        for entry in normalized_files:
            upload = entry["upload"]
            kind = str(entry["kind"])
            filename = str(entry["filename"])
            stem = str(entry["stem"])

            await upload.seek(0)
            file_bytes = await upload.read()

            logger.info(
                "Prepared conversion upload task_id=%s thread_id=%s filename=%s size=%s sha256=%s",
                task_id,
                thread_id,
                filename,
                len(file_bytes),
                _sha256_hex(file_bytes),
            )

            task_files_payload.append(
                {
                    "kind": kind,
                    "original_filename": filename,
                    "stem": stem,
                    "content_b64": base64.b64encode(file_bytes).decode("ascii"),
                }
            )

        convert_document_batch.apply_async(
            kwargs={
                "task_id": task_id,
                "thread_id": thread_id,
                "user_id": current_user.id,
                "kind": document_kind,
                "files": task_files_payload,
                "stems": stems,
                "lock_owner": lock_owner,
            },
            task_id=task_id,
        )
    except HTTPException:
        if session.in_transaction():
            session.rollback()
        release_thread_stems(thread_id=thread_id, kind=document_kind, stems=stems, owner=lock_owner)
        raise
    except Exception as exc:
        if session.in_transaction():
            session.rollback()
        release_thread_stems(thread_id=thread_id, kind=document_kind, stems=stems, owner=lock_owner)
        session.execute(delete(Job).where(Job.id == task_id))
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue conversion task: {exc}",
        ) from exc
    finally:
        await _close_uploads(normalized_files)

    accepted_files = [
        ConversionAcceptedFile(
            kind=str(entry["kind"]),
            filename=str(entry["filename"]),
            stem=str(entry["stem"]),
        )
        for entry in normalized_files
    ]
    return ConversionCreateResponse(
        task_id=task_id,
        status_url=str(
            request.url_for(
                "get_thread_conversion_status",
                thread_id=thread_id,
                task_id=task_id,
            )
        ),
        files=accepted_files,
    )


@router.get(
    "/threads/{thread_id}/conversions/{task_id}",
    response_model=ConversionStatusResponse,
    name="get_thread_conversion_status",
)
async def get_conversion_status(
    thread_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> ConversionStatusResponse:
    _require_owned_thread(session, user_id=current_user.id, thread_id=thread_id)
    job = _get_owned_job(
        session,
        user_id=current_user.id,
        thread_id=thread_id,
        task_id=task_id,
        kind=JOB_KIND_CONVERSION,
    )
    return _build_conversion_status_response(job)


@router.get("/threads/{thread_id}/messages", response_model=list[MessageResponse])
async def list_messages(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> list[MessageResponse]:
    _require_owned_thread(session, user_id=current_user.id, thread_id=thread_id)
    messages = session.execute(
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
    ).scalars()
    return [MessageResponse.model_validate(message, from_attributes=True) for message in messages]


@router.post(
    "/threads/{thread_id}/generations",
    response_model=GenerationCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_generation_task(
    thread_id: str,
    request: Request,
    generation_request: GenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> GenerationCreateResponse:
    period_description = generation_request.period_description.strip()
    if not period_description:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period description is required.",
        )

    task_id = str(uuid4())
    user_message_id = str(uuid4())

    try:
        thread = _require_owned_thread(
            session,
            user_id=current_user.id,
            thread_id=thread_id,
            for_update=True,
        )
        if _has_active_job(session, thread_id=thread_id, kind=JOB_KIND_GENERATION):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A generation task is already active for this thread.",
            )

        existing_kinds = set(
            session.execute(
                select(Document.kind).where(Document.thread_id == thread_id).distinct()
            ).scalars()
        )
        required_kinds = {DOCUMENT_KIND_ANALYTICS, DOCUMENT_KIND_SOURCES}
        if not required_kinds.issubset(existing_kinds):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Upload at least one analytics document and one sources document before "
                    "starting generation in this thread."
                ),
            )

        session.add(
            Message(
                id=user_message_id,
                thread_id=thread_id,
                role="user",
                content=period_description,
            )
        )
        thread.updated_at = utcnow()
        session.add(
            Job(
                id=task_id,
                user_id=current_user.id,
                thread_id=thread_id,
                kind=JOB_KIND_GENERATION,
                status=JOB_STATUS_QUEUED,
            )
        )
        session.commit()

        generate_analytics_by_period.apply_async(
            kwargs={
                "task_id": task_id,
                "thread_id": thread_id,
                "user_id": current_user.id,
                "period_description": period_description,
                "user_message_id": user_message_id,
            },
            task_id=task_id,
        )
    except HTTPException:
        if session.in_transaction():
            session.rollback()
        raise
    except Exception as exc:
        if session.in_transaction():
            session.rollback()
        session.execute(delete(Job).where(Job.id == task_id))
        session.execute(delete(Message).where(Message.id == user_message_id))
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue generation task: {exc}",
        ) from exc

    return GenerationCreateResponse(
        task_id=task_id,
        status_url=str(
            request.url_for(
                "get_thread_generation_status",
                thread_id=thread_id,
                task_id=task_id,
            )
        ),
        user_message_id=user_message_id,
    )


@router.get(
    "/threads/{thread_id}/generations/{task_id}",
    response_model=GenerationStatusResponse,
    name="get_thread_generation_status",
)
async def get_generation_status(
    thread_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> GenerationStatusResponse:
    _require_owned_thread(session, user_id=current_user.id, thread_id=thread_id)
    job = _get_owned_job(
        session,
        user_id=current_user.id,
        thread_id=thread_id,
        task_id=task_id,
        kind=JOB_KIND_GENERATION,
    )
    return _build_generation_status_response(job)
