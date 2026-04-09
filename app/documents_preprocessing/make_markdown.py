from pathlib import Path
from docling_core.types.doc import ImageRefMode
from app.documents_preprocessing.docling_converter import converter
from app.documents_preprocessing.prompt import make_prompt
from app.llm_utils import make_message, image_path_to_data_url
from app.schema import Role, Content, ContentType
from app.llm_client import LLMClient
import asyncio
import shutil
from app.documents_preprocessing.schema import Markdown
from app.llm_utils import get_response_content

MARKDOWNS_DIR = Path("markdowns")
MARKDOWNS_DIR.mkdir(parents=True, exist_ok=True)
MODEL = "qwen/qwen3-vl-32b-instruct"

async def get_image_description(image_path: Path, text_before: str, text_after: str) -> str:
    prompt = make_prompt(text_before=text_before, text_after=text_after)
    content = [
        Content(
            value=prompt,
            type=ContentType.text
        ),
        Content(
            value=image_path_to_data_url(image_path),
            type=ContentType.image_url
        )
    ]
    message = make_message(role=Role.user, content=content)
    async with LLMClient() as client:
        llm_response = await client.chat_completion(messages=[message], model=MODEL)
        description = get_response_content(llm_response)

    return description


async def make_markdown(pdf_path: Path, max_image_context_words: int = 3000) -> Markdown:
    result = converter.convert(pdf_path)
    md_path = MARKDOWNS_DIR /f"{pdf_path.stem}.md"
    images_dir_path = MARKDOWNS_DIR /f"{pdf_path.stem}_images"
    images_dir_path.mkdir(parents=True, exist_ok=True)
    result.document.save_as_markdown(
        md_path,
        image_mode=ImageRefMode.REFERENCED
    )

    with open(md_path, "r") as f:
        markdown = f.read()

    tasks = []
    image_tag = "![Image]"
    lines_to_replace: list[tuple[str, str]] = []
    png_count = 0
    for line in markdown.split("\n"):
        if image_tag not in line:
            continue
        image_path = MARKDOWNS_DIR / line.split(image_tag)[1][1:-1]
        line_start_position = markdown.find(line)
        line_end_position = line_start_position + len(line)
        text_before = markdown[max(0, line_start_position-max_image_context_words//2):line_start_position]
        text_after = markdown[line_end_position:line_end_position+max_image_context_words//2]

        tasks.append(get_image_description(image_path=image_path, text_before=text_before, text_after=text_after))

        new_image_path = f"{png_count}.png"
        png_count += 1
        shutil.copy(src=image_path, dst=images_dir_path/new_image_path)
        lines_to_replace.append((line, new_image_path))

    descriptions = await asyncio.gather(*tasks)
    assert len(descriptions) == len(lines_to_replace)
    for n in range(len(descriptions)):
        line_to_replace, link = lines_to_replace[n]
        desc = descriptions[n]
        new_line = f"""{image_tag}({link}) Краткое описание: ```{desc}```"""
        markdown = markdown.replace(line_to_replace, new_line)

    with open(md_path, "w") as f:
        f.write(markdown)


    return Markdown(
        markdown_path=md_path,
        images_dir_path=images_dir_path,
    )