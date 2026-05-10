from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import settings
from app.documents_preprocessing.docling_converter import (
    get_converter,
    ocr_backend_available,
)
from app.documents_preprocessing.prompt import (
    make_chart_detection_prompt,
    make_description_prompt,
    make_structured_prompt,
)
from app.documents_preprocessing.schema import (
    ImageAnalysis,
    ImageKindDetection,
    Markdown,
    parse_image_analysis,
    parse_image_kind_detection,
)
from app.llm_utils import get_response_content, image_path_to_data_url, make_message
from app.llm_client import LLMClient
from app.schema import Content, ContentType, Role

MARKDOWNS_DIR = Path(settings.MARKDOWNS_DIR)
MARKDOWNS_DIR.mkdir(parents=True, exist_ok=True)
CHART_DETECTION_MODEL = "qwen/qwen3-vl-32b-instruct"
CHART_TABLE_MODEL = "openai/gpt-5.4"
IMAGE_DESCRIPTION_MODEL = CHART_DETECTION_MODEL
IMAGE_TAG = "![Image]"
IMAGE_LINE_PATTERN = re.compile(r"!\[Image\]\((.+)\)$")
IMAGE_LINK_PATTERN = re.compile(r"!\[Image\]\(([^)]+)\)")
BULLET_GLYPH = ""
EMPTY_BULLET_LINE_PATTERN = re.compile(rf"(?m)^[ \t]*[-*][ \t]*{re.escape(BULLET_GLYPH)}?[ \t]*$")
BULLET_GLYPH_PREFIX_PATTERN = re.compile(rf"(?m)^([ \t]*[-*][ \t]*){re.escape(BULLET_GLYPH)}[ \t]+")
STANDALONE_BULLET_GLYPH_PATTERN = re.compile(rf"(?m)^[ \t]*{re.escape(BULLET_GLYPH)}[ \t]*$")
CHART_NOTE = "> Значения графика перенесены в таблицу приблизительно, по визуальной оценке."
DECORATIVE_IMAGE_PATTERNS = (
    "логотип",
    "global invest fund",
    "это не график и не визуализация данных",
    "а не график",
    "служебный элемент",
    "небольшой фрагмент с надписью",
    "маленький логотип",
    "декоративный элемент",
    "иконографический элемент",
    "абстрактный графический элемент",
    "абстрактный пиксельный узор",
    "не являющийся графиком",
    "не является графиком",
    "не график, диаграмма или визуализация данных",
)
OcrMode = Literal["off", "auto", "force"]


@dataclass(slots=True)
class _ImageLine:
    index: int
    source_path: Path
    text_before: str
    text_after: str


@dataclass(slots=True)
class _ImageReplacement:
    line_index: int
    markdown: str
    source_path: Path | None = None


def _build_output_paths(source_path: Path, output_dir: Path | None) -> tuple[Path, Path, Path]:
    base_dir = MARKDOWNS_DIR if output_dir is None else output_dir.expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = base_dir / f"{source_path.stem}.md"
    images_dir_path = base_dir / f"{source_path.stem}_images"
    artifacts_dir_path = base_dir / f"{source_path.stem}_artifacts"
    return markdown_path, images_dir_path, artifacts_dir_path


def _extract_image_path(line: str) -> str | None:
    match = IMAGE_LINE_PATTERN.fullmatch(line.strip())
    if match is None:
        return None
    return match.group(1)


def _resolve_image_path(raw_path: str, markdown_path: Path) -> Path:
    image_path = Path(raw_path)
    if not image_path.is_absolute():
        image_path = markdown_path.parent / image_path
    return image_path.expanduser().resolve()


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ").strip()


def _render_markdown_table(columns: list[str], rows: list[list[str]]) -> str:
    normalized_columns = [_escape_markdown_cell(column) for column in columns]
    header = f"| {' | '.join(normalized_columns)} |"
    separator = f"| {' | '.join(['---'] * len(normalized_columns))} |"
    body = [
        f"| {' | '.join(_escape_markdown_cell(cell) for cell in row)} |"
        for row in rows
    ]
    return "\n".join([header, separator, *body])


def _render_chart_block(analysis: ImageAnalysis) -> str:
    parts: list[str] = []
    if analysis.title:
        parts.append(f"**График: {analysis.title}**")
        parts.append("")

    parts.append(_render_markdown_table(columns=analysis.columns, rows=analysis.rows))
    parts.append("")
    parts.append(CHART_NOTE)
    return "\n".join(parts)


def _render_image_block(image_link: str, description: str) -> str:
    normalized_description = _normalize_text(description)
    return f"{IMAGE_TAG}({image_link})\n\nКраткое описание: {normalized_description}"


def _is_decorative_image_description(description: str | None) -> bool:
    if description is None:
        return False
    normalized = _normalize_text(description).lower()
    return any(pattern in normalized for pattern in DECORATIVE_IMAGE_PATTERNS)


def _cleanup_markdown(markdown: str) -> str:
    cleaned = BULLET_GLYPH_PREFIX_PATTERN.sub(r"\1", markdown)
    cleaned = STANDALONE_BULLET_GLYPH_PATTERN.sub("", cleaned)
    cleaned = EMPTY_BULLET_LINE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def _count_meaningful_words(markdown: str) -> int:
    diagnostic = IMAGE_LINK_PATTERN.sub(" ", markdown)
    diagnostic = diagnostic.replace(BULLET_GLYPH, " ")
    return len(re.findall(r"\w+", diagnostic, flags=re.UNICODE))


def _looks_like_broken_text_extraction(markdown: str) -> bool:
    meaningful_words = _count_meaningful_words(markdown)
    bullet_placeholders = markdown.count(BULLET_GLYPH)
    return meaningful_words <= 120 or (
        meaningful_words <= 300 and bullet_placeholders >= 8
    )


def _convert_with_docling(
    *,
    source: Path,
    markdown_path: Path,
    use_ocr: bool,
    force_full_page_ocr: bool,
) -> None:
    from docling_core.types.doc import ImageRefMode

    result = get_converter(
        use_ocr=use_ocr,
        force_full_page_ocr=force_full_page_ocr,
    ).convert(source)
    result.document.save_as_markdown(markdown_path, image_mode=ImageRefMode.REFERENCED)


def _prune_unreferenced_images(markdown: str, images_dir_path: Path) -> None:
    referenced_names: set[str] = set()
    for match in IMAGE_LINK_PATTERN.finditer(markdown):
        link = match.group(1)
        if "/" not in link:
            continue
        dir_name, file_name = link.rsplit("/", 1)
        if dir_name == images_dir_path.name:
            referenced_names.add(file_name)

    for image_path in images_dir_path.iterdir():
        if image_path.is_file() and image_path.name not in referenced_names:
            image_path.unlink()


async def _call_model(prompt: str, image_path: Path, model: str) -> str:
    content = [
        Content(value=prompt, type=ContentType.text),
        Content(value=image_path_to_data_url(image_path), type=ContentType.image_url),
    ]
    message = make_message(role=Role.user, content=content)
    async with LLMClient() as client:
        llm_response = await client.chat_completion(messages=[message], model=model)
    return get_response_content(llm_response).strip()


async def _detect_chart(image_path: Path, text_before: str, text_after: str) -> ImageKindDetection:
    llm_content = await _call_model(
        prompt=make_chart_detection_prompt(text_before=text_before, text_after=text_after),
        image_path=image_path,
        model=CHART_DETECTION_MODEL,
    )
    return parse_image_kind_detection(llm_content)


async def _analyze_image(image_path: Path, text_before: str, text_after: str) -> ImageAnalysis:
    llm_content = await _call_model(
        prompt=make_structured_prompt(text_before=text_before, text_after=text_after),
        image_path=image_path,
        model=CHART_TABLE_MODEL,
    )
    return parse_image_analysis(llm_content)


async def _describe_image(image_path: Path, text_before: str, text_after: str) -> str:
    return await _call_model(
        prompt=make_description_prompt(text_before=text_before, text_after=text_after),
        image_path=image_path,
        model=IMAGE_DESCRIPTION_MODEL,
    )


async def _build_image_replacement(image_line: _ImageLine) -> _ImageReplacement:
    detection: ImageKindDetection | None = None
    try:
        detection = await _detect_chart(
            image_path=image_line.source_path,
            text_before=image_line.text_before,
            text_after=image_line.text_after,
        )
    except Exception:
        detection = None

    analysis: ImageAnalysis | None = None
    if detection is not None and detection.kind == "chart":
        try:
            analysis = await _analyze_image(
                image_path=image_line.source_path,
                text_before=image_line.text_before,
                text_after=image_line.text_after,
            )
        except Exception:
            analysis = None

    if analysis is not None and analysis.can_render_chart_table():
        return _ImageReplacement(
            line_index=image_line.index,
            markdown=_render_chart_block(analysis),
        )

    description = analysis.description if analysis is not None else None
    if description is None and detection is not None:
        description = detection.description

    if _is_decorative_image_description(description):
        return _ImageReplacement(
            line_index=image_line.index,
            markdown="",
        )

    if description is None:
        try:
            description = await _describe_image(
                image_path=image_line.source_path,
                text_before=image_line.text_before,
                text_after=image_line.text_after,
            )
        except Exception:
            description = "Не удалось автоматически описать изображение."

    if _is_decorative_image_description(description):
        return _ImageReplacement(
            line_index=image_line.index,
            markdown="",
        )

    return _ImageReplacement(
        line_index=image_line.index,
        markdown=description,
        source_path=image_line.source_path,
    )


async def make_markdown(
    source_path: Path,
    max_image_context_words: int = 3000,
    output_dir: Path | None = None,
    ocr_mode: OcrMode = "auto",
) -> Markdown:
    source = source_path.expanduser().resolve()
    markdown_path, images_dir_path, artifacts_dir_path = _build_output_paths(
        source_path=source,
        output_dir=output_dir,
    )
    markdown_existed = markdown_path.exists()
    images_dir_existed = images_dir_path.exists()
    images_dir_path.mkdir(parents=True, exist_ok=True)

    try:
        if ocr_mode == "force":
            _convert_with_docling(
                source=source,
                markdown_path=markdown_path,
                use_ocr=True,
                force_full_page_ocr=True,
            )
        else:
            _convert_with_docling(
                source=source,
                markdown_path=markdown_path,
                use_ocr=False,
                force_full_page_ocr=False,
            )
        markdown = markdown_path.read_text(encoding="utf-8")
        if (
            ocr_mode == "auto"
            and _looks_like_broken_text_extraction(markdown)
            and ocr_backend_available()
        ):
            if artifacts_dir_path.exists():
                shutil.rmtree(artifacts_dir_path, ignore_errors=True)
            _convert_with_docling(
                source=source,
                markdown_path=markdown_path,
                use_ocr=True,
                force_full_page_ocr=True,
            )
            markdown = markdown_path.read_text(encoding="utf-8")
        lines = markdown.split("\n")
        image_lines: list[_ImageLine] = []
        cursor = 0

        for index, line in enumerate(lines):
            raw_image_path = _extract_image_path(line)
            line_start_position = cursor
            line_end_position = line_start_position + len(line)
            cursor = line_end_position + 1

            if raw_image_path is None:
                continue

            text_before = markdown[max(0, line_start_position - max_image_context_words // 2):line_start_position]
            text_after = markdown[line_end_position:line_end_position + max_image_context_words // 2]
            image_lines.append(
                _ImageLine(
                    index=index,
                    source_path=_resolve_image_path(raw_path=raw_image_path, markdown_path=markdown_path),
                    text_before=text_before,
                    text_after=text_after,
                )
            )

        replacements = await asyncio.gather(
            *[_build_image_replacement(image_line=image_line) for image_line in image_lines]
        )

        next_image_index = 0
        for replacement in replacements:
            if replacement.source_path is None:
                lines[replacement.line_index] = replacement.markdown
                continue

            suffix = replacement.source_path.suffix.lower() or ".png"
            new_image_name = f"{next_image_index}{suffix}"
            next_image_index += 1
            destination = images_dir_path / new_image_name
            shutil.copy(src=replacement.source_path, dst=destination)
            lines[replacement.line_index] = _render_image_block(
                image_link=f"{images_dir_path.name}/{new_image_name}",
                description=replacement.markdown,
            )

        final_markdown = _cleanup_markdown("\n".join(lines))
        markdown_path.write_text(final_markdown, encoding="utf-8")
        _prune_unreferenced_images(markdown=final_markdown, images_dir_path=images_dir_path)
    except BaseException:
        if not markdown_existed and markdown_path.exists():
            markdown_path.unlink()
        if not images_dir_existed and images_dir_path.exists():
            shutil.rmtree(images_dir_path, ignore_errors=True)
        if artifacts_dir_path.exists():
            shutil.rmtree(artifacts_dir_path, ignore_errors=True)
        raise
    else:
        if artifacts_dir_path.exists():
            shutil.rmtree(artifacts_dir_path, ignore_errors=True)

    return Markdown(markdown_path=markdown_path, images_dir_path=images_dir_path)
