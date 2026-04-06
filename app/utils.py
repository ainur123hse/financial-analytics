from pathlib import Path
import fitz
import re

PAGE_REFERENCE_PATTERN = re.compile(r"\[p\.\d+\]")

def _validate_pdf_path(path_to_pdf: Path) -> Path:
    pdf_path = path_to_pdf.expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {pdf_path}")

    try:
        with fitz.open(pdf_path) as document:
            _ = document.page_count
    except Exception as exc:  # pragma: no cover - library exception types vary
        raise ValueError(f"Failed to open PDF file: {pdf_path}") from exc
    return pdf_path


def _validation_error(parsed_answer: dict[str, str], final_only: bool) -> str | None:
    if parsed_answer["kind"] == "invalid":
        return parsed_answer["error"]
    if final_only and parsed_answer["kind"] != "final_answer":
        return "Only <final_answer> is allowed at this stage."
    if parsed_answer["kind"] == "final_answer" and PAGE_REFERENCE_PATTERN.search(
        parsed_answer["final_answer"]
    ) is None:
        return "Final answer must contain at least one page reference in the format [p.N]."
    return None
