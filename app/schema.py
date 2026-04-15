from enum import Enum
from pydantic import BaseModel


class Role(Enum):
    user = "user"
    assistant = "assistant"
    system = "system"

class ContentType(Enum):
    image_url = "image_url"
    text = "text"

class Content(BaseModel):
    value: str
    type: ContentType

class CodeExecutionResult(BaseModel):
    success: bool
    exception_with_traceback: str | None = None
    stdout: str

class ImageQuestion(BaseModel):
    image_path: str
    question: str

class MainAgentContent(BaseModel):
    code_to_execute: str | None
    dependencies: list[str] | None
    image_questions: list[ImageQuestion] | None
    final_answer: str | None
