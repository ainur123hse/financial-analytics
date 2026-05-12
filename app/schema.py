from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class ContentType(str, Enum):
    image_url = "image_url"
    text = "text"


class Content(BaseModel):
    value: str
    type: ContentType
