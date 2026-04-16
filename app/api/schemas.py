from pydantic import BaseModel, Field


class ConversionAcceptedFile(BaseModel):
    filename: str
    stem: str


class ConversionCreateResponse(BaseModel):
    task_id: str
    status_url: str
    files: list[ConversionAcceptedFile]


class ConversionItemResult(BaseModel):
    filename: str
    stem: str
    markdown_path: str | None = None
    images_dir_path: str | None = None
    error: str | None = None


class ConversionStatusResponse(BaseModel):
    task_id: str
    status: str
    items: list[ConversionItemResult] = Field(default_factory=list)
    error: str | None = None


class QARequest(BaseModel):
    question: str = Field(min_length=1)


class QAResponse(BaseModel):
    answer: str
