import json

from pydantic import ValidationError

from app.schema import MainAgentContent


def _load_first_json_object(raw_content: str) -> dict:
    decoder = json.JSONDecoder()
    cursor = 0
    parsed_values: list[object] = []

    while cursor < len(raw_content):
        while cursor < len(raw_content) and raw_content[cursor].isspace():
            cursor += 1
        if cursor >= len(raw_content):
            break

        try:
            value, cursor = decoder.raw_decode(raw_content, cursor)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Main agent returned invalid JSON: {exc.msg}") from exc

        parsed_values.append(value)

    if not parsed_values:
        raise ValueError("Main agent returned an empty response.")

    first_payload = parsed_values[0]
    if not isinstance(first_payload, dict):
        raise ValueError("Main agent JSON root must be an object.")

    return first_payload


def parse_main_agent_content(llm_content: str) -> MainAgentContent:
    raw_content = llm_content.strip()
    if not raw_content:
        raise ValueError("Main agent returned an empty response.")

    if raw_content.startswith("```"):
        raise ValueError("Main agent must return strict JSON without markdown fences.")

    payload = _load_first_json_object(raw_content=raw_content)

    if payload.get("image_questions") == []:
        payload = dict(payload)
        payload["image_questions"] = None

    try:
        content = MainAgentContent.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Main agent JSON does not match schema: {exc}") from exc

    has_final_answer = content.final_answer is not None
    has_code = content.code_to_execute is not None
    has_image_questions = content.image_questions is not None

    if has_final_answer and (has_code or has_image_questions):
        raise ValueError("final_answer cannot be used together with code_to_execute or image_questions.")
    if not has_final_answer and not (has_code or has_image_questions):
        raise ValueError("Either final_answer or at least one action field must be provided.")
    if not has_code and content.dependencies is not None:
        raise ValueError("dependencies must be null when code_to_execute is null.")

    return content
