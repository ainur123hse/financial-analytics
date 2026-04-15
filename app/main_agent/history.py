from app.llm_utils import make_message
from app.schema import CodeExecutionResult, Role, Content, ContentType

def new_history_messages(
    main_agent_content: str,
    code_res: CodeExecutionResult | None,
    image_qa_res: list[tuple[str, str, str]] | None
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
        for index, (image_path, question, answer) in enumerate(image_qa_res, start=1):
            observations.append(
                "\n".join(
                    [
                        f"Ответ на вопрос по изображению #{index}:",
                        f"image_path: {image_path}",
                        f"question: {question}",
                        f"answer:\n{answer}",
                    ]
                )
            )

    if not observations:
        raise ValueError("No tool output was provided to continue the dialogue.")

    tool_result_message = make_message(
        role=Role.user,
        content=[Content(value="\n\n".join(observations), type=ContentType.text)],
    )

    return [assistant_message, tool_result_message]
