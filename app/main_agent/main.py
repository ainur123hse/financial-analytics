from app.llm_client import LLMClient
from app.llm_utils import make_message, get_response_content
from app.schema import Role, ContentType, Content, MainAgentContent, CodeExecutionResult
from app.main_agent.system_prompt import make_system_prompt
from app.code_tool.execute_code import execute_python_code_and_parse_result
from app.image_question_answering_tool.answer import answer_by_image
from pathlib import Path
import json
import shutil
from pydantic import ValidationError
import asyncio

MODEL = "openai/gpt-5.4"
MAX_ITERATIONS = 6

CODE_EXECUTION_WORKING_DIR = Path("code_execution_working_dir")
CODE_EXECUTION_WORKING_DIR.mkdir(parents=True, exist_ok=True)
VIRTUAL_ENV_PATH = CODE_EXECUTION_WORKING_DIR / ".venv"

def parse_main_agent_content(llm_content: str) -> MainAgentContent:
    raw_content = llm_content.strip()
    if not raw_content:
        raise ValueError("Main agent returned an empty response.")

    if raw_content.startswith("```"):
        raise ValueError("Main agent must return strict JSON without markdown fences.")

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Main agent returned invalid JSON: {exc.msg}") from exc

    try:
        content = MainAgentContent.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Main agent JSON does not match schema: {exc}") from exc


    has_final_answer = content.final_answer is not None
    has_code = content.code_to_execute is not None
    has_image_question = content.image_question is not None

    if has_final_answer and (has_code or has_image_question):
        raise ValueError("final_answer cannot be used together with code_to_execute or image_question.")
    if not has_final_answer and not (has_code or has_image_question):
        raise ValueError("Either final_answer or at least one action field must be provided.")
    if not has_code and content.dependencies is not None:
        raise ValueError("dependencies must be null when code_to_execute is null.")

    return content

def prepare_code_execution_working_dir(
    md_path: Path,
    md_images_dir_path: Path,
    code_execution_working_dir: Path
) -> None:
    source_md_path = Path(md_path).expanduser().resolve()
    source_images_dir_path = Path(md_images_dir_path).expanduser().resolve()

    if not source_md_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {source_md_path}")
    if not source_images_dir_path.is_dir():
        raise FileNotFoundError(f"Images directory not found: {source_images_dir_path}")

    if code_execution_working_dir.exists():
        if code_execution_working_dir.is_dir():
            shutil.rmtree(code_execution_working_dir)
        else:
            code_execution_working_dir.unlink()
    code_execution_working_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(source_md_path, code_execution_working_dir / source_md_path.name)
    for image_path in source_images_dir_path.iterdir():
        if image_path.is_file():
            shutil.copy(image_path, code_execution_working_dir / image_path.name)

def new_history_messages(
    main_agent_content: str,
    code_res: CodeExecutionResult | None,
    image_qa_res: str | None
)-> list[dict]:
    assistant_message = make_message(
        role=Role.assistant,
        content=[Content(value=main_agent_content, type=ContentType.text)],
    )

    observations: list[str] = []
    if code_res is not None:
        code_observation_lines = [
            "Результат выполнения Python-кода:",
            f"success: {code_res.success}",
            f"stdout:\n{code_res.stdout or '<empty>'}",
        ]
        if code_res.exception_with_traceback:
            code_observation_lines.append(
                f"exception_with_traceback:\n{code_res.exception_with_traceback}"
            )
        observations.append("\n".join(code_observation_lines))

    if image_qa_res is not None:
        observations.append(f"Ответ на вопрос по изображению:\n{image_qa_res}")

    if not observations:
        raise ValueError("No tool output was provided to continue the dialogue.")

    tool_result_message = make_message(
        role=Role.user,
        content=[Content(value="\n\n".join(observations), type=ContentType.text)],
    )

    return [assistant_message, tool_result_message]

async def answer_to_question(user_question: str, md_path: Path, md_images_dir_path: Path) -> str:
    prepare_code_execution_working_dir(
        md_path=md_path,
        md_images_dir_path=md_images_dir_path,
        code_execution_working_dir=CODE_EXECUTION_WORKING_DIR
    )
    sys_message = make_message(
        role=Role.system,
        content=[Content(value=make_system_prompt(md_path.name), type=ContentType.text)]
    )
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
            assert (main_agent_parsed_content.code_to_execute is None) and (main_agent_parsed_content.image_question is None)
            return main_agent_parsed_content.final_answer

        assert main_agent_parsed_content.code_to_execute or main_agent_parsed_content.image_question

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

        if main_agent_parsed_content.image_question:
            image_qa_res = await answer_by_image(
                image_path=CODE_EXECUTION_WORKING_DIR / main_agent_parsed_content.image_question.image_path,
                question=main_agent_parsed_content.image_question.question
            )

        history += new_history_messages(main_agent_content=main_agent_content, code_res=code_res, image_qa_res=image_qa_res)

    return "Нет результата"