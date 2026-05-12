from __future__ import annotations

import shutil
from pathlib import Path

from app.config import settings
from app.models import DOCUMENT_KIND_ANALYTICS, DOCUMENT_KIND_SOURCES

MARKDOWNS_ROOT = Path(settings.MARKDOWNS_DIR)
UPLOADED_PDFS_ROOT = Path(settings.UPLOADED_PDFS_DIR)
THREADS_DIRNAME = "threads"
ANALYTICS_DIRNAME = "аналитика"
DOCUMENTS_DIRNAME = "документы"
DOCUMENT_KIND_DIRNAMES = {
    DOCUMENT_KIND_ANALYTICS: ANALYTICS_DIRNAME,
    DOCUMENT_KIND_SOURCES: DOCUMENTS_DIRNAME,
}

MARKDOWNS_ROOT.mkdir(parents=True, exist_ok=True)
UPLOADED_PDFS_ROOT.mkdir(parents=True, exist_ok=True)


def thread_markdowns_dir(thread_id: str) -> Path:
    return MARKDOWNS_ROOT / THREADS_DIRNAME / thread_id


def thread_uploads_dir(thread_id: str) -> Path:
    return UPLOADED_PDFS_ROOT / THREADS_DIRNAME / thread_id


def thread_uploaded_batch_dir(thread_id: str, task_id: str) -> Path:
    return thread_uploads_dir(thread_id) / task_id


def document_kind_dirname(kind: str) -> str:
    try:
        return DOCUMENT_KIND_DIRNAMES[kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported document kind: {kind}") from exc


def thread_documents_dir(thread_id: str, kind: str) -> Path:
    return thread_markdowns_dir(thread_id) / document_kind_dirname(kind)


def thread_markdown_path(thread_id: str, kind: str, stem: str) -> Path:
    return thread_documents_dir(thread_id, kind) / f"{stem}.md"


def thread_images_dir(thread_id: str, kind: str, stem: str) -> Path:
    return thread_documents_dir(thread_id, kind) / f"{stem}_images"


def thread_artifacts_dir(thread_id: str, kind: str, stem: str) -> Path:
    return thread_documents_dir(thread_id, kind) / f"{stem}_artifacts"


def ensure_thread_dirs(thread_id: str) -> None:
    thread_markdowns_dir(thread_id).mkdir(parents=True, exist_ok=True)
    thread_uploads_dir(thread_id).mkdir(parents=True, exist_ok=True)
    for kind in DOCUMENT_KIND_DIRNAMES:
        thread_documents_dir(thread_id, kind).mkdir(parents=True, exist_ok=True)


def delete_document_files(
    *,
    thread_id: str,
    kind: str,
    markdown_filename: str,
    images_dirname: str,
    artifacts_dirname: str,
) -> None:
    thread_dir = thread_documents_dir(thread_id, kind)
    markdown_path = thread_dir / markdown_filename
    images_dir_path = thread_dir / images_dirname
    artifacts_dir_path = thread_dir / artifacts_dirname

    if markdown_path.exists():
        markdown_path.unlink()
    if images_dir_path.exists():
        shutil.rmtree(images_dir_path, ignore_errors=True)
    if artifacts_dir_path.exists():
        shutil.rmtree(artifacts_dir_path, ignore_errors=True)


def delete_thread_storage(thread_id: str) -> None:
    shutil.rmtree(thread_markdowns_dir(thread_id), ignore_errors=True)
    shutil.rmtree(thread_uploads_dir(thread_id), ignore_errors=True)
