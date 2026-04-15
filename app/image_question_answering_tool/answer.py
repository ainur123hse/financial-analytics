from app.llm_client import LLMClient
from app.llm_utils import make_message, image_path_to_data_url, get_response_content
from app.schema import Role, ContentType, Content
from pathlib import Path

MODEL = "qwen/qwen3-vl-32b-instruct"

async def answer_by_image(image_path: Path, question: str) -> str:
    image_url = image_path_to_data_url(image_path)
    image_content = Content(
        value=image_url,
        type=ContentType.image_url
    )
    question_content = Content(
        value=question,
        type=ContentType.text
    )
    async with LLMClient() as client:
        system_message = make_message(
            role=Role.system, content=[Content(value="При ответах на вопрос опирайся только на содержание изображения", type=ContentType.text)]
        )
        message = make_message(
            role=Role.user, content=[image_content, question_content]
        )
        llm_response = await client.chat_completion(messages=[system_message, message], model=MODEL)

    return get_response_content(llm_response)