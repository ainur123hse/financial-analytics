from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.documents_preprocessing.docling_converter import (
    is_supported_source_path,
    supported_source_extensions_label,
)
from app.documents_preprocessing.make_markdown import OcrMode, make_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a supported document to markdown and transform chart-like images into tables when possible.",
    )
    parser.add_argument(
        "source_path",
        type=Path,
        help=f"Path to the source document ({supported_source_extensions_label()}).",
    )
    parser.add_argument(
        "--max-image-context-words",
        type=int,
        default=3000,
        help="Approximate character window used as text context around each image.",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("off", "auto", "force"),
        default="auto",
        help="PDF OCR mode: disabled, automatic fallback on suspiciously empty output, or forced full-page OCR.",
    )
    return parser


async def _run(source_path: Path, max_image_context_words: int, ocr_mode: OcrMode) -> None:
    source = source_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")
    if not source.is_file():
        raise ValueError(f"Path is not a file: {source}")
    if not is_supported_source_path(source):
        raise ValueError(
            f"Only {supported_source_extensions_label()} files are supported: {source}"
        )

    markdown = await make_markdown(
        source_path=source,
        max_image_context_words=max_image_context_words,
        ocr_mode=ocr_mode,
    )
    print(f"markdown_path={markdown.markdown_path}")
    print(f"images_dir_path={markdown.images_dir_path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_image_context_words <= 0:
        parser.error("--max-image-context-words must be > 0")

    try:
        asyncio.run(_run(args.source_path, args.max_image_context_words, args.ocr_mode))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
