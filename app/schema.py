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