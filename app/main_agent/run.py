import asyncio
from pathlib import Path

from app.code_tool.utils import prepare_code_execution_working_dir
from app.llm_client import LLMClient
from app.llm_utils import make_message, get_response_content
from app.main_agent.history import new_history_messages
from app.main_agent.parse import parse_main_agent_content
from app.schema import Role, ContentType, Content, ImageQuestion
from app.main_agent.system_prompt import make_system_prompt
from app.code_tool.execute_code import execute_python_code_and_parse_result
from app.image_question_answering_tool.answer import answer_by_image

MODEL = "openai/gpt-5.4"
MAX_ITERATIONS = 6

CODE_EXECUTION_WORKING_DIR = Path("code_execution_working_dir")
CODE_EXECUTION_WORKING_DIR.mkdir(parents=True, exist_ok=True)
VIRTUAL_ENV_PATH = CODE_EXECUTION_WORKING_DIR / ".venv"


async def _answer_image_questions(image_questions: list[ImageQuestion]) -> list[tuple[str, str, str]]:
    answers = await asyncio.gather(
        *[
            answer_by_image(
                image_path=CODE_EXECUTION_WORKING_DIR / image_question.image_path,
                question=image_question.question,
            )
            for image_question in image_questions
        ]
    )
    return [
        (image_question.image_path, image_question.question, answer)
        for image_question, answer in zip(image_questions, answers, strict=True)
    ]


async def answer_to_question(user_question: str, md_dir_path: Path) -> str:

    prepare_code_execution_working_dir(
        md_dir_path=md_dir_path,
        code_execution_working_dir=CODE_EXECUTION_WORKING_DIR
    )
    sys_message = make_message(
        role=Role.system,
        content=[Content(value=make_system_prompt(md_dir_path), type=ContentType.text)]
    )

    # print(sys_message["content"][0]["text"])


    user_question_message = make_message(
        role=Role.user,
        content=[Content(value=user_question, type=ContentType.text)]
    )
    history = [sys_message, user_question_message]
    for iteration in range(MAX_ITERATIONS):
        async with LLMClient() as client:
            main_agent_response = await client.chat_completion(messages=history, model=MODEL)

        main_agent_content = get_response_content(response=main_agent_response)
        main_agent_parsed_content = parse_main_agent_content(llm_content=main_agent_content)
        if main_agent_parsed_content.final_answer:
            assert (main_agent_parsed_content.code_to_execute is None) and (main_agent_parsed_content.image_questions is None)
            return main_agent_parsed_content.final_answer

        assert main_agent_parsed_content.code_to_execute or main_agent_parsed_content.image_questions

        code_res = None
        image_qa_res = None

        if main_agent_parsed_content.code_to_execute:
            dependencies = []
            if main_agent_parsed_content.dependencies is not None:
                dependencies = main_agent_parsed_content.dependencies
            code_res = execute_python_code_and_parse_result(
                python_code=main_agent_parsed_content.code_to_execute,
                dependencies=dependencies,
                working_dir_path=CODE_EXECUTION_WORKING_DIR,
                virtual_env_path=VIRTUAL_ENV_PATH,
            )

        if main_agent_parsed_content.image_questions:
            image_qa_res = await _answer_image_questions(
                image_questions=main_agent_parsed_content.image_questions
            )

        history += new_history_messages(main_agent_content=main_agent_content, code_res=code_res, image_qa_res=image_qa_res)

    return "Нет результата"
