from __future__ import annotations

from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

SUPPORTED_SOURCE_EXTENSIONS = (".pdf", ".html", ".htm")
_SUPPORTED_SOURCE_EXTENSIONS_SET = frozenset(SUPPORTED_SOURCE_EXTENSIONS)


def is_supported_source_path(path: Path) -> bool:
    return path.suffix.lower() in _SUPPORTED_SOURCE_EXTENSIONS_SET


def supported_source_extensions_label() -> str:
    return ", ".join(SUPPORTED_SOURCE_EXTENSIONS)


def ocr_backend_label() -> str | None:
    if find_spec("onnxruntime") is not None:
        return "rapidocr"
    if which("tesseract") is not None:
        return "tesseract"
    return None


def ocr_backend_available() -> bool:
    return ocr_backend_label() is not None


@lru_cache(maxsize=8)
def get_converter(
    *,
    use_ocr: bool = False,
    force_full_page_ocr: bool = False,
) -> "DocumentConverter":
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        RapidOcrOptions,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, HTMLFormatOption, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = use_ocr
    pipeline_options.generate_picture_images = True
    pipeline_options.generate_page_images = False
    if use_ocr:
        backend = ocr_backend_label()
        if backend == "rapidocr":
            pipeline_options.ocr_options = RapidOcrOptions(
                force_full_page_ocr=force_full_page_ocr,
                lang=["ru", "en"],
            )
        elif backend == "tesseract":
            pipeline_options.ocr_options = TesseractCliOcrOptions(
                force_full_page_ocr=force_full_page_ocr,
                lang=["rus", "eng"],
            )
        else:
            raise RuntimeError(
                "OCR is requested, but no OCR backend is available. "
                "Install onnxruntime for RapidOCR or add the tesseract binary."
            )

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.HTML],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.HTML: HTMLFormatOption(),
        }
    )
