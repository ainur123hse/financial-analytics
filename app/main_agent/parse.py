import json
from pydantic import ValidationError
from app.schema import MainAgentContent


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
