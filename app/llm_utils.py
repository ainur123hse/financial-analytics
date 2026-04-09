from app.schema import Role, ContentType, Content
import base64
import mimetypes
from pathlib import Path
from typing import Any

def make_message(role: Role, content: list[Content]) -> dict[str, Any]:

    content_dict = []
    for elem in content:
        value = elem.value
        if elem.type == ContentType.image_url:
            value = {
                "url": elem.value
            }
        content_dict.append({
            "type": elem.type.value,
            elem.type.value: value
        })


    return {
        "role": role.value,
        "content": content_dict
    }

def get_response_content(response: Any) -> str:
    return response.choices[0].message.content

def image_path_to_data_url(image_path: Path) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None or not mime_type.startswith("image/"):
        raise ValueError(f"Unsupported image type: {path}")

    encoded_bytes = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded_bytes}"