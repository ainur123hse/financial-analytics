from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.documents_preprocessing.make_markdown import make_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to markdown and enrich image blocks with short descriptions.",
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        help="Path to the source PDF file.",
    )
    parser.add_argument(
        "--max-image-context-words",
        type=int,
        default=3000,
        help="Approximate character window used as text context around each image.",
    )
    return parser


async def _run(pdf_path: Path, max_image_context_words: int) -> None:
    source = pdf_path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"PDF file not found: {source}")
    if not source.is_file():
        raise ValueError(f"Path is not a file: {source}")
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"Only .pdf files are supported: {source}")

    markdown = await make_markdown(
        pdf_path=source,
        max_image_context_words=max_image_context_words,
    )
    print(f"markdown_path={markdown.markdown_path}")
    print(f"images_dir_path={markdown.images_dir_path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_image_context_words <= 0:
        parser.error("--max-image-context-words must be > 0")

    try:
        asyncio.run(_run(args.pdf_path, args.max_image_context_words))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
