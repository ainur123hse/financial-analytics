from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator

class Markdown(BaseModel):
    markdown_path: Path
    images_dir_path: Path


class ImageKindDetection(BaseModel):
    kind: Literal["chart", "other"]
    description: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class ImageAnalysis(BaseModel):
    kind: Literal["chart", "other"]
    title: str | None = None
    description: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    approximate: bool = False

    @field_validator("title", "description", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("columns", mode="before")
    @classmethod
    def _normalize_columns(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise TypeError("columns must be a list.")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("rows", mode="before")
    @classmethod
    def _normalize_rows(cls, value: Any) -> list[list[str]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise TypeError("rows must be a list.")

        rows: list[list[str]] = []
        for row in value:
            if not isinstance(row, list):
                raise TypeError("Each row must be a list.")
            normalized_row = [str(cell).strip() for cell in row]
            if any(cell for cell in normalized_row):
                rows.append(normalized_row)
        return rows

    def can_render_chart_table(self) -> bool:
        if self.kind != "chart":
            return False
        if len(self.columns) < 2 or len(self.rows) < 2:
            return False
        return all(len(row) == len(self.columns) for row in self.rows)


def _load_first_json_object(raw_content: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    cursor = 0
    parsed_values: list[object] = []

    while cursor < len(raw_content):
        while cursor < len(raw_content) and raw_content[cursor].isspace():
            cursor += 1
        if cursor >= len(raw_content):
            break

        value, cursor = decoder.raw_decode(raw_content, cursor)
        parsed_values.append(value)

    if not parsed_values:
        raise ValueError("Image analysis response is empty.")

    first_payload = parsed_values[0]
    if not isinstance(first_payload, dict):
        raise ValueError("Image analysis JSON root must be an object.")

    return first_payload


def _parse_payload(raw_content: str) -> dict[str, Any]:
    payload_text = raw_content.strip()
    if not payload_text:
        raise ValueError("Image analysis response is empty.")
    if payload_text.startswith("```"):
        raise ValueError("Image analysis response must not contain markdown fences.")

    try:
        payload = _load_first_json_object(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Image analysis returned invalid JSON: {exc.msg}") from exc

    return payload


def parse_image_kind_detection(raw_content: str) -> ImageKindDetection:
    return ImageKindDetection.model_validate(_parse_payload(raw_content))


def parse_image_analysis(raw_content: str) -> ImageAnalysis:
    return ImageAnalysis.model_validate(_parse_payload(raw_content))
