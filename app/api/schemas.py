from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AuthRegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class AuthUserResponse(BaseModel):
    id: str
    email: str
    created_at: datetime


class ThreadCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ThreadUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ThreadResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class DocumentResponse(BaseModel):
    id: str
    kind: str
    original_filename: str
    stem: str
    created_at: datetime


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime


class ConversionAcceptedFile(BaseModel):
    kind: str
    filename: str
    stem: str


class ConversionCreateResponse(BaseModel):
    task_id: str
    status_url: str
    files: list[ConversionAcceptedFile]


class ConversionItemResult(BaseModel):
    kind: str
    filename: str
    stem: str
    document_id: str | None = None
    error: str | None = None


class ConversionStatusResponse(BaseModel):
    task_id: str
    status: str
    items: list[ConversionItemResult] = Field(default_factory=list)
    error: str | None = None


class GenerationRequest(BaseModel):
    period_description: str = Field(min_length=1, max_length=10000)


class GenerationCreateResponse(BaseModel):
    task_id: str
    status_url: str
    user_message_id: str


class GenerationStatusResponse(BaseModel):
    task_id: str
    status: str
    analysis_markdown: str | None = None
    error: str | None = None
    assistant_message_id: str | None = None
