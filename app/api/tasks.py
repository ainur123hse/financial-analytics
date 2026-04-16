from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.api.celery_app import celery_app
from app.api.redis_store import remove_active_task, release_stems
from app.config import settings


MARKDOWNS_DIR = Path(settings.MARKDOWNS_DIR)


def _rollback_batch_outputs(stems: list[str]) -> None:
    for stem in stems:
        markdown_path = MARKDOWNS_DIR / f"{stem}.md"
        images_dir_path = MARKDOWNS_DIR / f"{stem}_images"

        if markdown_path.exists():
            markdown_path.unlink()
        if images_dir_path.exists() and images_dir_path.is_dir():
            shutil.rmtree(images_dir_path)


@celery_app.task(name="conversions.convert_pdf_batch", bind=True)
def convert_pdf_batch(
    self,
    task_id: str,
    files: list[dict[str, str]],
    stems: list[str],
    lock_owner: str,
) -> dict[str, object]:
    # Delay heavy docling/torch imports until the first real conversion task.
    from app.documents_preprocessing.make_markdown import make_markdown

    items: list[dict[str, str | None]] = []
    processed_stems: set[str] = set()

    try:
        for file_meta in files:
            filename = file_meta["original_filename"]
            stem = file_meta["stem"]
            pdf_path = Path(file_meta["source_path"])

            try:
                markdown = asyncio.run(make_markdown(pdf_path=pdf_path))
                items.append(
                    {
                        "filename": filename,
                        "stem": stem,
                        "markdown_path": str(markdown.markdown_path),
                        "images_dir_path": str(markdown.images_dir_path),
                        "error": None,
                    }
                )
                processed_stems.add(stem)
            except Exception as exc:
                failure_message = f"{filename}: {exc}"
                items.append(
                    {
                        "filename": filename,
                        "stem": stem,
                        "markdown_path": None,
                        "images_dir_path": None,
                        "error": failure_message,
                    }
                )
                _rollback_batch_outputs(stems=stems)

                for item in items:
                    if item["stem"] in processed_stems:
                        item["markdown_path"] = None
                        item["images_dir_path"] = None
                        item["error"] = "Rolled back because another file in this batch failed."

                seen_stems = {item["stem"] for item in items}
                for skipped_meta in files:
                    skipped_stem = skipped_meta["stem"]
                    if skipped_stem in seen_stems:
                        continue
                    items.append(
                        {
                            "filename": skipped_meta["original_filename"],
                            "stem": skipped_stem,
                            "markdown_path": None,
                            "images_dir_path": None,
                            "error": "Skipped because batch processing already failed.",
                        }
                    )

                return {
                    "task_id": task_id,
                    "status": "failed",
                    "items": items,
                    "error": failure_message,
                }

        return {
            "task_id": task_id,
            "status": "completed",
            "items": items,
            "error": None,
        }
    except Exception as exc:
        _rollback_batch_outputs(stems=stems)
        return {
            "task_id": task_id,
            "status": "failed",
            "items": items,
            "error": str(exc),
        }
    finally:
        try:
            release_stems(stems=stems, owner=lock_owner)
        finally:
            remove_active_task(task_id=task_id)
