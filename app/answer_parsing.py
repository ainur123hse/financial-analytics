import re
from typing import Any

TOP_LEVEL_TAG_PATTERN = re.compile(
    r"<(?P<tag>code|final_answer)>\s*(?P<body>.*?)\s*</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
CODE_BODY_PATTERN = re.compile(
    r"^\s*<dependencies>\s*(?P<dependencies>.*?)\s*</dependencies>\s*"
    r"<script>\s*(?P<script>.*?)\s*</script>\s*$",
    re.IGNORECASE | re.DOTALL,
)
DEPENDENCY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:(?:==|!=|<=|>=|<|>|~=)[A-Za-z0-9][A-Za-z0-9*._+-]*)?$"
)
FENCE_PATTERN = re.compile(
    r"^\s*```(?:[a-zA-Z0-9_+-]+)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL,
)


def parse_llm_answer(answer: str) -> dict[str, Any]:
    cleaned = _strip_outer_fences(answer)
    matches = list(TOP_LEVEL_TAG_PATTERN.finditer(cleaned))
    if len(matches) != 1:
        return {
            "kind": "invalid",
            "error": "Expected exactly one top-level tag: <code> or <final_answer>.",
            "raw_answer": answer,
        }

    match = matches[0]
    prefix = cleaned[: match.start()].strip()
    suffix = cleaned[match.end() :].strip()
    if prefix or suffix:
        return {
            "kind": "invalid",
            "error": "Response contains extra text outside the required tag.",
            "raw_answer": answer,
        }

    body = match.group("body").strip()
    if not body:
        return {
            "kind": "invalid",
            "error": f"Tag <{match.group('tag').lower()}> must not be empty.",
            "raw_answer": answer,
        }

    tag = match.group("tag").lower()
    if tag == "code":
        return _parse_code_answer(body, answer)
    return {"kind": "final_answer", "final_answer": body}


def _parse_code_answer(body: str, raw_answer: str) -> dict[str, Any]:
    match = CODE_BODY_PATTERN.match(body)
    if match is None:
        return {
            "kind": "invalid",
            "error": (
                "Code response must contain exactly <dependencies>...</dependencies> "
                "followed by <script>...</script>."
            ),
            "raw_answer": raw_answer,
        }

    script = match.group("script").strip()
    if not script:
        return {
            "kind": "invalid",
            "error": "Tag <script> must not be empty.",
            "raw_answer": raw_answer,
        }

    dependencies: list[str] = []
    for line_number, line in enumerate(match.group("dependencies").splitlines(), start=1):
        dependency = line.strip()
        if not dependency:
            continue
        if DEPENDENCY_PATTERN.fullmatch(dependency) is None:
            return {
                "kind": "invalid",
                "error": (
                    "Dependency line "
                    f"{line_number} has unsupported format: {dependency!r}. "
                    "Use only package names or package+version specifiers."
                ),
                "raw_answer": raw_answer,
            }
        dependencies.append(dependency)

    return {
        "kind": "code",
        "code": script,
        "dependencies": dependencies,
    }


def _strip_outer_fences(answer: str) -> str:
    cleaned = answer.strip()
    while True:
        match = FENCE_PATTERN.match(cleaned)
        if match is None:
            return cleaned
        cleaned = match.group("body").strip()
