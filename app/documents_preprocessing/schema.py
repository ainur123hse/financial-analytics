from pydantic import BaseModel
from pathlib import Path

class Markdown(BaseModel):
    markdown_path: Path
    images_dir_path: Path