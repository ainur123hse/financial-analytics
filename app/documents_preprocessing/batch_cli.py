from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from app.documents_preprocessing.docling_converter import (
    is_supported_source_path,
    supported_source_extensions_label,
)
from app.documents_preprocessing.make_markdown import make_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert supported documents in a directory to markdown and turn chart-like images into tables when possible.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help=f"Directory that contains supported documents ({supported_source_extensions_label()}) on the top level.",
    )
    parser.add_argument(
        "--max-image-context-words",
        type=int,
        default=3000,
        help="Approximate character window used as text context around each image.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recreate markdown and image outputs even if they already exist.",
    )
    return parser


async def _run(input_dir: Path, max_image_context_words: int, force: bool) -> int:
    source_dir = input_dir.expanduser().resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"Path is not a directory: {source_dir}")

    source_paths = sorted(path for path in source_dir.iterdir() if path.is_file() and is_supported_source_path(path))
    if not source_paths:
        print(f"No supported files found in {source_dir}")
        return 0

    processed = 0
    skipped = 0
    failed = 0

    for source_path in source_paths:
        markdown_path = source_path.with_suffix(".md")
        images_dir_path = source_path.parent / f"{source_path.stem}_images"
        artifacts_dir_path = source_path.parent / f"{source_path.stem}_artifacts"

        if force:
            if markdown_path.exists():
                markdown_path.unlink()
            if images_dir_path.exists():
                shutil.rmtree(images_dir_path, ignore_errors=True)
            if artifacts_dir_path.exists():
                shutil.rmtree(artifacts_dir_path, ignore_errors=True)
        elif not markdown_path.exists():
            if images_dir_path.exists():
                shutil.rmtree(images_dir_path, ignore_errors=True)
            if artifacts_dir_path.exists():
                shutil.rmtree(artifacts_dir_path, ignore_errors=True)

        if not force and (markdown_path.exists() or images_dir_path.exists()):
            skipped += 1
            print(f"SKIPPED {source_path.name}: output already exists")
            continue

        try:
            markdown = await make_markdown(
                source_path=source_path,
                max_image_context_words=max_image_context_words,
                output_dir=source_path.parent,
                ocr_mode="off",
            )
        except Exception as exc:
            failed += 1
            print(f"FAILED  {source_path.name}: {exc}")
            continue

        processed += 1
        print(f"OK      {source_path.name}: {markdown.markdown_path}")

    print("")
    print(f"processed={processed}")
    print(f"skipped={skipped}")
    print(f"failed={failed}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_image_context_words <= 0:
        parser.error("--max-image-context-words must be > 0")

    try:
        return asyncio.run(_run(args.input_dir, args.max_image_context_words, args.force))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
