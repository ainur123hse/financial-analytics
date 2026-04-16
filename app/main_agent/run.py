import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from app.code_tool.execute_code import execute_python_code_and_parse_result
from app.code_tool.utils import prepare_code_execution_working_dir
from app.image_question_answering_tool.answer import answer_by_image
from app.langfuse_client import (
    flush_langfuse,
    safe_update_observation,
    start_observation_context,
    trace_attributes_context,
)
from app.llm_client import LLMClient
from app.llm_utils import get_response_content, make_message
from app.main_agent.history import new_history_messages
from app.main_agent.parse import parse_main_agent_content
from app.main_agent.system_prompt import make_system_prompt
from app.schema import CodeExecutionResult, Content, ContentType, ImageQuestion, Role

MODEL = "openai/gpt-5.4"
MAX_ITERATIONS = 6

CODE_EXECUTION_WORKING_DIR = Path("code_execution_working_dir")
CODE_EXECUTION_WORKING_DIR.mkdir(parents=True, exist_ok=True)


def _serialize_code_result(code_res: CodeExecutionResult) -> dict[str, str | bool | None]:
    return {
        "success": code_res.success,
        "stdout": code_res.stdout,
        "exception_with_traceback": code_res.exception_with_traceback,
    }


def _serialize_image_answers(image_qa_res: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [
        {
            "image_path": image_path,
            "question": question,
            "answer": answer,
        }
        for image_path, question, answer in image_qa_res
    ]


async def _answer_image_questions(
    image_questions: list[ImageQuestion],
    working_dir_path: Path,
) -> list[tuple[str, str, str]]:
    answers = await asyncio.gather(
        *[
            answer_by_image(
                image_path=working_dir_path / image_question.image_path,
                question=image_question.question,
            )
            for image_question in image_questions
        ]
    )
    return [
        (image_question.image_path, image_question.question, answer)
        for image_question, answer in zip(image_questions, answers, strict=True)
    ]


async def _answer_to_question_impl(
    user_question: str,
    md_dir_path: Path,
    working_dir_path: Path,
) -> str:
    prepare_code_execution_working_dir(
        md_dir_path=md_dir_path,
        code_execution_working_dir=working_dir_path,
    )
    sys_message = make_message(
        role=Role.system,
        content=[Content(value=make_system_prompt(working_dir_path), type=ContentType.text)],
    )

    user_question_message = make_message(
        role=Role.user,
        content=[Content(value=user_question, type=ContentType.text)],
    )
    history = [sys_message, user_question_message]

    for iteration in range(MAX_ITERATIONS):
        iteration_index = iteration + 1
        with start_observation_context(
            name="main_agent.iteration",
            as_type="chain",
            input={
                "iteration": iteration_index,
                "history_message_count": len(history),
            },
            metadata={"model": MODEL},
        ) as iteration_observation:
            try:
                async with LLMClient() as client:
                    main_agent_response = await client.chat_completion(
                        messages=history,
                        model=MODEL,
                        langfuse_name="main_agent.plan_and_decide",
                        langfuse_metadata={
                            "component": "main_agent",
                            "iteration": iteration_index,
                            "history_message_count": len(history),
                        },
                    )

                main_agent_content = get_response_content(response=main_agent_response)
                main_agent_parsed_content = parse_main_agent_content(llm_content=main_agent_content)
                if main_agent_parsed_content.final_answer:
                    assert (main_agent_parsed_content.code_to_execute is None) and (
                        main_agent_parsed_content.image_questions is None
                    )
                    safe_update_observation(
                        iteration_observation,
                        output={
                            "status": "final_answer",
                            "final_answer": main_agent_parsed_content.final_answer,
                        },
                    )
                    return main_agent_parsed_content.final_answer

                assert (
                    main_agent_parsed_content.code_to_execute
                    or main_agent_parsed_content.image_questions
                )

                code_res = None
                image_qa_res = None

                if main_agent_parsed_content.code_to_execute:
                    dependencies = []
                    if main_agent_parsed_content.dependencies is not None:
                        dependencies = main_agent_parsed_content.dependencies

                    with start_observation_context(
                        name="main_agent.execute_python_code",
                        as_type="tool",
                        input={
                            "python_code": main_agent_parsed_content.code_to_execute,
                            "dependencies": dependencies,
                        },
                    ) as code_observation:
                        code_res = execute_python_code_and_parse_result(
                            python_code=main_agent_parsed_content.code_to_execute,
                            dependencies=dependencies,
                            working_dir_path=working_dir_path,
                            virtual_env_path=working_dir_path / ".venv",
                        )
                        safe_update_observation(
                            code_observation,
                            output=_serialize_code_result(code_res=code_res),
                            level="ERROR" if not code_res.success else None,
                            status_message=(
                                "Python code execution failed."
                                if not code_res.success
                                else None
                            ),
                        )

                if main_agent_parsed_content.image_questions:
                    image_questions_payload = [
                        {
                            "image_path": image_question.image_path,
                            "question": image_question.question,
                        }
                        for image_question in main_agent_parsed_content.image_questions
                    ]
                    with start_observation_context(
                        name="main_agent.answer_image_questions",
                        as_type="tool",
                        input=image_questions_payload,
                    ) as image_observation:
                        image_qa_res = await _answer_image_questions(
                            image_questions=main_agent_parsed_content.image_questions,
                            working_dir_path=working_dir_path,
                        )
                        safe_update_observation(
                            image_observation,
                            output=_serialize_image_answers(image_qa_res=image_qa_res),
                        )

                history += new_history_messages(
                    main_agent_content=main_agent_content,
                    code_res=code_res,
                    image_qa_res=image_qa_res,
                )
                safe_update_observation(
                    iteration_observation,
                    output={
                        "status": "continue",
                        "history_message_count": len(history),
                        "ran_code_execution": code_res is not None,
                        "ran_image_qa": image_qa_res is not None,
                    },
                )
            except Exception as exc:
                safe_update_observation(
                    iteration_observation,
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

    return "Нет результата"


async def answer_to_question(user_question: str, md_dir_path: Path) -> str:
    session_id = f"main-agent-{uuid4()}"
    request_working_dir = CODE_EXECUTION_WORKING_DIR / session_id
    root_observation = None
    try:
        with start_observation_context(
            name="main_agent.answer_to_question",
            as_type="agent",
            input={
                "user_question": user_question,
                "md_dir_path": str(md_dir_path),
            },
            metadata={
                "model": MODEL,
                "max_iterations": MAX_ITERATIONS,
            },
        ) as root_observation:
            with trace_attributes_context(
                session_id=session_id,
                metadata={
                    "component": "main_agent",
                    "model": MODEL,
                },
                tags=["main_agent"],
                trace_name="main_agent.answer_to_question",
            ):
                answer = await _answer_to_question_impl(
                    user_question=user_question,
                    md_dir_path=md_dir_path,
                    working_dir_path=request_working_dir,
                )

            safe_update_observation(
                root_observation,
                output={"final_answer": answer},
            )
            return answer
    except Exception as exc:
        safe_update_observation(
            root_observation,
            level="ERROR",
            status_message=str(exc),
        )
        raise
    finally:
        if request_working_dir.exists():
            shutil.rmtree(request_working_dir, ignore_errors=True)
        flush_langfuse()
