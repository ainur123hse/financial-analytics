from __future__ import annotations

import io
import json
import logging
from pathlib import Path
import re
import shlex
import tarfile
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.codex_runner.prompt import build_generation_prompt
from app.codex_runner.config import WORKSPACE_DIR, SOURCE_DIR, RESULT_PATH, GENERATED_ANALYSIS_PATH, SCHEMA_PATH, \
    COPY_CONTAINER_PREFIX, RUNNER_CONTAINER_PREFIX, WORKSPACE_VOLUME_PREFIX, CONTAINER_POLL_INTERVAL_SECONDS, \
    MAX_LOG_CHARS, RUNNER_EXTRA_HOSTS, EXEC_MARKER, CODEX_MARKER, USER_MARKER, TOKENS_USED_MARKER, \
    TIMESTAMPED_LOG_LINE_PATTERN, RFC3339_TIMESTAMP_PATTERN, EXEC_STATUS_PATTERN, EXEC_COMMAND_PATTERN, \
    REASONING_HEADING_PATTERN, MARKER_MESSAGES
from app.config import settings

logger = logging.getLogger(__name__)


class GenerationOutput(BaseModel):
    status_message: str


class CodexTraceStep(BaseModel):
    step_type: Literal["reasoning", "exec", "final_output"]
    name: str
    start_time: datetime
    end_time: datetime
    input: Any | None = None
    output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    level: str | None = None


class GenerationCodexRunResult(BaseModel):
    analysis_markdown: str
    runner_container_name: str | None = None
    codex_session_id: str | None = None
    codex_model: str | None = None
    steps: list[CodexTraceStep] = Field(default_factory=list)


class _TimestampedLogLine(BaseModel):
    timestamp: datetime
    message: str


class _ParsedRunnerLogs(BaseModel):
    codex_session_id: str | None = None
    codex_model: str | None = None
    steps: list[CodexTraceStep] = Field(default_factory=list)


def _resolve_openrouter_api_key() -> str:
    return settings.OPENROUTER_API_KEY or settings.LLM_API_KEY


def build_codex_exec_command(period_description: str) -> list[str]:
    return [
        "codex",
        "--search",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        WORKSPACE_DIR,
        "--output-schema",
        SCHEMA_PATH,
        "-o",
        RESULT_PATH,
        build_generation_prompt(period_description),
    ]


def _build_codex_runner_environment(openrouter_api_key: str) -> dict[str, str]:
    return {"OPENROUTER_API_KEY": openrouter_api_key}


def _build_codex_runner_shell_command(
    period_description: str,
) -> str:
    return "\n".join(
        [
            "set -eu",
            f"exec {shlex.join(build_codex_exec_command(period_description=period_description))}",
        ]
    )


def _make_volume_mount(source: str, target: str, read_only: bool = False) -> Any:
    from docker.types import Mount

    return Mount(target=target, source=source, type="volume", read_only=read_only)


def _wait_for_container_exit(container: Any, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State", {})
        if not state.get("Running", False):
            return int(state.get("ExitCode") or 0)
        time.sleep(CONTAINER_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Container `{container.name}` did not finish within {timeout_seconds} seconds."
    )


def _read_single_file_from_tar_bytes(archive_bytes: bytes, filename: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.rstrip("/").endswith(filename):
                extracted = tar.extractfile(member)
                if extracted is None:
                    break
                return extracted.read().decode("utf-8")
    raise FileNotFoundError(f"File `{filename}` was not found in container archive.")


def _read_text_file_from_container(container: Any, path: str) -> str:
    stream, _ = container.get_archive(path)
    archive_bytes = b"".join(stream)
    return _read_single_file_from_tar_bytes(
        archive_bytes=archive_bytes,
        filename=Path(path).name,
    )


def _decode_container_logs(raw_logs: Any) -> str:
    if isinstance(raw_logs, bytes):
        return raw_logs.decode("utf-8", errors="replace")
    return str(raw_logs)


def _safe_read_logs(container: Any | None, *, max_chars: int | None = MAX_LOG_CHARS) -> str:
    if container is None:
        return ""
    try:
        raw_logs = container.logs(stdout=True, stderr=True)
    except Exception:
        return ""

    logs = _decode_container_logs(raw_logs)
    logs = logs.strip()
    if (max_chars is not None) and len(logs) > max_chars:
        overflow = len(logs) - max_chars
        logs = f"<truncated {overflow} chars>\n{logs[-max_chars:]}"
    return logs


def _remove_container(container: Any | None) -> None:
    if container is None:
        return
    try:
        container.remove(force=True)
    except Exception:
        pass


def _remove_volume(volume: Any | None) -> None:
    if volume is None:
        return
    try:
        volume.remove(force=True)
    except Exception:
        pass


def _read_timestamped_logs(container: Any | None) -> str:
    if container is None:
        return ""
    try:
        raw_logs = container.logs(stdout=True, stderr=True, timestamps=True)
    except Exception:
        return ""
    return _decode_container_logs(raw_logs)


def _parse_rfc3339_timestamp(raw_timestamp: str) -> datetime:
    match = RFC3339_TIMESTAMP_PATTERN.match(raw_timestamp)
    if match is None:
        raise ValueError(f"Unsupported Docker log timestamp format: `{raw_timestamp}`")

    fraction = (match.group("fraction") or "")[:6].ljust(6, "0")
    timezone_suffix = "+00:00" if match.group("tz") == "Z" else match.group("tz")
    normalized = f"{match.group('prefix')}.{fraction}{timezone_suffix}"
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _parse_timestamped_log_lines(raw_logs: str) -> list[_TimestampedLogLine]:
    parsed_lines: list[_TimestampedLogLine] = []
    for raw_line in raw_logs.splitlines():
        match = TIMESTAMPED_LOG_LINE_PATTERN.match(raw_line)
        if match is None:
            if not parsed_lines:
                continue
            parsed_lines.append(
                _TimestampedLogLine(
                    timestamp=parsed_lines[-1].timestamp,
                    message=raw_line,
                )
            )
            continue

        parsed_lines.append(
            _TimestampedLogLine(
                timestamp=_parse_rfc3339_timestamp(match.group("timestamp")),
                message=match.group("message") or "",
            )
        )
    return parsed_lines


def _extract_header_metadata(
    log_lines: list[_TimestampedLogLine],
) -> tuple[dict[str, str], int | None]:
    metadata: dict[str, str] = {}
    separator_count = 0
    user_index: int | None = None

    for index, line in enumerate(log_lines):
        message = line.message
        if message == "--------":
            separator_count += 1
            continue
        if message == USER_MARKER:
            user_index = index
            break
        if separator_count == 1 and ":" in message:
            key, value = message.split(":", 1)
            metadata[key.strip().lower()] = value.strip()

    return metadata, user_index


def _find_payload_start_index(
    log_lines: list[_TimestampedLogLine],
    *,
    user_index: int | None,
    period_description: str,
) -> int:
    if user_index is None:
        return 0

    index = user_index + 1
    prompt_lines = build_generation_prompt(period_description).splitlines()
    matched_lines = 0
    while (
        matched_lines < len(prompt_lines)
        and index < len(log_lines)
        and log_lines[index].message == prompt_lines[matched_lines]
    ):
        index += 1
        matched_lines += 1

    if matched_lines == len(prompt_lines):
        while index < len(log_lines) and log_lines[index].message == "":
            index += 1
        return index

    probe_index = user_index + 1
    while probe_index < len(log_lines):
        if log_lines[probe_index].message.strip() in MARKER_MESSAGES:
            return probe_index
        probe_index += 1

    return index


def _parse_exec_command(command_line: str) -> tuple[str, str | None]:
    match = EXEC_COMMAND_PATTERN.match(command_line.strip())
    if match is None:
        return command_line.strip(), None
    return match.group("command"), match.group("cwd")


def _parse_duration_ms(raw_duration: str, unit: str) -> int | None:
    try:
        numeric_duration = float(raw_duration)
    except ValueError:
        return None
    multiplier = 1 if unit == "ms" else 1000
    return int(numeric_duration * multiplier)


def _parse_exec_status_line(status_line: str) -> tuple[str, int | None, int | None]:
    match = EXEC_STATUS_PATTERN.match(status_line)
    if match is None:
        return status_line.strip() or "unknown", None, None

    if match.group("status") == "succeeded":
        return "succeeded", 0, _parse_duration_ms(match.group("duration"), match.group("unit"))

    exit_code_raw = match.group("exit_code")
    exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    return "exited", exit_code, _parse_duration_ms(match.group("duration"), match.group("unit"))


def _lines_to_text(lines: list[_TimestampedLogLine]) -> str:
    return "\n".join(line.message for line in lines).strip()


def _looks_like_reasoning_start(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if REASONING_HEADING_PATTERN.match(text) or text.startswith("#"):
        return True
    if text.startswith(("/", "$", "|", ">", "{", "[")):
        return False
    if re.match(r"^[A-Za-zА-Яа-я].{8,}$", text) is None:
        return False
    return any(char in text for char in (".", "!", "?", ":"))


def _build_reasoning_step_name(reasoning_text: str) -> str:
    first_line = reasoning_text.splitlines()[0].strip() if reasoning_text.strip() else ""
    heading_match = REASONING_HEADING_PATTERN.match(first_line)
    if heading_match is not None:
        return heading_match.group("title")
    if first_line.startswith("#"):
        return first_line.lstrip("#").strip() or "generation.codex_runner.reasoning"
    return "generation.codex_runner.reasoning"


def _parse_exec_step(
    log_lines: list[_TimestampedLogLine],
    start_index: int,
) -> tuple[CodexTraceStep | None, int]:
    marker_line = log_lines[start_index]
    current_index = start_index + 1
    if current_index >= len(log_lines):
        return None, current_index

    command_line = log_lines[current_index].message
    command_timestamp = log_lines[current_index].timestamp
    current_index += 1

    status_line = ""
    status_timestamp = command_timestamp
    if current_index < len(log_lines):
        status_line = log_lines[current_index].message
        status_timestamp = log_lines[current_index].timestamp
        current_index += 1

    status, exit_code, reported_duration_ms = _parse_exec_status_line(status_line)
    output_lines: list[_TimestampedLogLine] = []
    last_timestamp = status_timestamp

    while current_index < len(log_lines):
        current_line = log_lines[current_index]
        stripped_message = current_line.message.strip()
        if stripped_message in MARKER_MESSAGES:
            break
        if current_line.message == "":
            next_index = current_index + 1
            while next_index < len(log_lines) and log_lines[next_index].message == "":
                next_index += 1
            if next_index >= len(log_lines):
                current_index = next_index
                break
            if log_lines[next_index].message.strip() in MARKER_MESSAGES or _looks_like_reasoning_start(
                log_lines[next_index].message
            ):
                current_index = next_index
                break

        output_lines.append(current_line)
        last_timestamp = current_line.timestamp
        current_index += 1

    command, working_directory = _parse_exec_command(command_line)
    output_text = _lines_to_text(output_lines)
    return (
        CodexTraceStep(
            step_type="exec",
            name="generation.codex_runner.exec",
            start_time=marker_line.timestamp,
            end_time=last_timestamp,
            input={
                "command": command,
                "cwd": working_directory,
            },
            output={"text": output_text or "<empty>"},
            metadata={
                "status": status,
                "exit_code": exit_code,
                "reported_duration_ms": reported_duration_ms,
                "timing_source": "docker_stdout_timestamps",
            },
            level="ERROR" if exit_code not in (None, 0) else None,
        ),
        current_index,
    )


def _parse_reasoning_step(
    log_lines: list[_TimestampedLogLine],
    start_index: int,
) -> tuple[CodexTraceStep | None, int]:
    current_index = start_index
    reasoning_lines: list[_TimestampedLogLine] = []
    while current_index < len(log_lines):
        current_line = log_lines[current_index]
        if current_line.message.strip() in MARKER_MESSAGES:
            break
        reasoning_lines.append(current_line)
        current_index += 1

    reasoning_text = _lines_to_text(reasoning_lines)
    if not reasoning_text:
        return None, current_index

    return (
        CodexTraceStep(
            step_type="reasoning",
            name=_build_reasoning_step_name(reasoning_text),
            start_time=reasoning_lines[0].timestamp,
            end_time=reasoning_lines[-1].timestamp,
            output={"text": reasoning_text},
            metadata={
                "approximate_timing": True,
                "timing_source": "docker_stdout_timestamps",
            },
        ),
        current_index,
    )


def _parse_final_output_step(
    log_lines: list[_TimestampedLogLine],
    start_index: int,
    *,
    fallback_status_message: str,
) -> tuple[CodexTraceStep, int]:
    marker_line = log_lines[start_index]
    current_index = start_index + 1
    final_lines = log_lines[current_index:]
    parsed_payloads: list[dict[str, Any]] = []
    tokens_used: int | None = None

    while current_index < len(log_lines):
        message = log_lines[current_index].message.strip()
        if message.lower() == TOKENS_USED_MARKER and current_index + 1 < len(log_lines):
            token_text = log_lines[current_index + 1].message.replace(",", "").strip()
            if token_text.isdigit():
                tokens_used = int(token_text)
            current_index += 2
            continue

        if message.startswith("{") and message.endswith("}"):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                current_index += 1
                continue
            if isinstance(payload, dict) and "status_message" in payload:
                parsed_payloads.append(payload)
        current_index += 1

    deduplicated_payloads: list[dict[str, Any]] = []
    for payload in parsed_payloads:
        if payload not in deduplicated_payloads:
            deduplicated_payloads.append(payload)

    final_payload = (
        deduplicated_payloads[-1]
        if deduplicated_payloads
        else {"status_message": fallback_status_message}
    )
    if (
        not isinstance(final_payload.get("status_message"), str)
        or not final_payload["status_message"].strip()
    ):
        final_payload["status_message"] = fallback_status_message

    metadata: dict[str, Any] = {
        "timing_source": "docker_stdout_timestamps",
        "duplicate_payload_count": max(len(parsed_payloads) - len(deduplicated_payloads), 0),
    }
    if tokens_used is not None:
        metadata["tokens_used"] = tokens_used

    end_time = final_lines[-1].timestamp if final_lines else marker_line.timestamp
    return (
        CodexTraceStep(
            step_type="final_output",
            name="generation.codex_runner.final_output",
            start_time=marker_line.timestamp,
            end_time=end_time,
            output={"status_message": final_payload["status_message"]},
            metadata=metadata,
        ),
        len(log_lines),
    )


def _parse_codex_runner_logs(
    *,
    raw_logs: str,
    period_description: str,
    fallback_status_message: str,
) -> _ParsedRunnerLogs:
    log_lines = _parse_timestamped_log_lines(raw_logs)
    if not log_lines:
        return _ParsedRunnerLogs()

    header_metadata, user_index = _extract_header_metadata(log_lines)
    payload_start_index = _find_payload_start_index(
        log_lines,
        user_index=user_index,
        period_description=period_description,
    )

    current_index = payload_start_index
    steps: list[CodexTraceStep] = []
    while current_index < len(log_lines):
        stripped_message = log_lines[current_index].message.strip()
        if not stripped_message:
            current_index += 1
            continue

        if stripped_message == EXEC_MARKER:
            step, current_index = _parse_exec_step(log_lines, current_index)
            if step is not None:
                steps.append(step)
            continue

        if stripped_message == CODEX_MARKER:
            final_step, current_index = _parse_final_output_step(
                log_lines,
                current_index,
                fallback_status_message=fallback_status_message,
            )
            steps.append(final_step)
            continue

        step, current_index = _parse_reasoning_step(log_lines, current_index)
        if step is not None:
            steps.append(step)

    return _ParsedRunnerLogs(
        codex_session_id=header_metadata.get("session id"),
        codex_model=header_metadata.get("model"),
        steps=steps,
    )


def _run_copy_container(
    client: Any,
    task_id: str,
    workspace_volume_name: str,
    source_subpath: str,
) -> Any:
    normalized_source_subpath = source_subpath.strip("/")
    source_path = f"{SOURCE_DIR}/{normalized_source_subpath}"
    return client.containers.run(
        image=settings.QA_RUNNER_IMAGE,
        name=f"{COPY_CONTAINER_PREFIX}-{task_id}",
        command=[
            "sh",
            "-lc",
            " && ".join(
                [
                    f"mkdir -p {shlex.quote(WORKSPACE_DIR)}",
                    f"cp -a {shlex.quote(source_path)}/. {shlex.quote(WORKSPACE_DIR)}/",
                ]
            ),
        ],
        mounts=[
            _make_volume_mount(
                source=settings.MARKDOWNS_DOCKER_VOLUME,
                target=SOURCE_DIR,
                read_only=True,
            ),
            _make_volume_mount(source=workspace_volume_name, target=WORKSPACE_DIR),
        ],
        network_disabled=True,
        detach=True,
    )


def _run_codex_container(
    client: Any,
    task_id: str,
    workspace_volume_name: str,
    period_description: str,
    openrouter_api_key: str,
) -> Any:
    return client.containers.run(
        image=settings.QA_RUNNER_IMAGE,
        name=f"{RUNNER_CONTAINER_PREFIX}-{task_id}",
        command=[
            "sh",
            "-lc",
            _build_codex_runner_shell_command(period_description=period_description),
        ],
        mounts=[_make_volume_mount(source=workspace_volume_name, target=WORKSPACE_DIR)],
        environment=_build_codex_runner_environment(openrouter_api_key=openrouter_api_key),
        extra_hosts=RUNNER_EXTRA_HOSTS,
        detach=True,
    )


def _validate_generation_output(raw_content: str) -> str:
    try:
        payload = GenerationOutput.model_validate(json.loads(raw_content))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"Codex returned invalid generation JSON: {exc}") from exc

    status_message = payload.status_message.strip()
    if not status_message:
        raise RuntimeError("Codex returned an empty status message.")
    return status_message


def _validate_generated_analysis_markdown(raw_content: str) -> str:
    analysis_markdown = raw_content.strip()
    if not analysis_markdown:
        raise RuntimeError("Codex returned an empty generated analysis file.")
    return analysis_markdown


def run_generation_codex_in_docker(
    task_id: str,
    period_description: str,
    source_subpath: str = "",
) -> GenerationCodexRunResult:
    openrouter_api_key = _resolve_openrouter_api_key()

    try:
        import docker

        client = docker.from_env()
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize Docker client: {exc}") from exc

    workspace_volume_name = f"{WORKSPACE_VOLUME_PREFIX}-{task_id}"
    workspace_volume: Any | None = None
    copy_container: Any | None = None
    runner_container: Any | None = None
    try:
        workspace_volume = client.volumes.create(
            name=workspace_volume_name,
            labels={
                "financial_analytics.kind": "generation_workspace",
                "financial_analytics.task_id": task_id,
            },
        )

        copy_container = _run_copy_container(
            client=client,
            task_id=task_id,
            workspace_volume_name=workspace_volume_name,
            source_subpath=source_subpath,
        )
        copy_exit_code = _wait_for_container_exit(
            container=copy_container,
            timeout_seconds=int(settings.QA_COPY_CONTAINER_TIMEOUT_SECONDS),
        )
        if copy_exit_code != 0:
            copy_logs = _safe_read_logs(copy_container)
            raise RuntimeError(
                "Failed to prepare generation workspace snapshot."
                f" Exit code: {copy_exit_code}. Logs: {copy_logs or '<empty>'}"
            )

        runner_container = _run_codex_container(
            client=client,
            task_id=task_id,
            workspace_volume_name=workspace_volume_name,
            period_description=period_description,
            openrouter_api_key=openrouter_api_key,
        )
        runner_exit_code = _wait_for_container_exit(
            container=runner_container,
            timeout_seconds=int(settings.QA_CONTAINER_TIMEOUT_SECONDS),
        )
        if runner_exit_code != 0:
            runner_logs = _safe_read_logs(runner_container)
            raise RuntimeError(
                "Codex generation container failed."
                f" Exit code: {runner_exit_code}. Logs: {runner_logs or '<empty>'}"
            )

        try:
            raw_content = _read_text_file_from_container(
                container=runner_container,
                path=RESULT_PATH,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Codex did not write result file `{RESULT_PATH}`."
            ) from exc

        status_message = _validate_generation_output(raw_content=raw_content)
        try:
            generated_analysis_raw = _read_text_file_from_container(
                container=runner_container,
                path=GENERATED_ANALYSIS_PATH,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Codex did not write generated analysis file `{GENERATED_ANALYSIS_PATH}`."
            ) from exc

        analysis_markdown = _validate_generated_analysis_markdown(generated_analysis_raw)
        runner_logs = _read_timestamped_logs(runner_container)
        parsed_logs = _ParsedRunnerLogs()
        if runner_logs:
            try:
                parsed_logs = _parse_codex_runner_logs(
                    raw_logs=runner_logs,
                    period_description=period_description,
                    fallback_status_message=status_message,
                )
            except Exception:
                logger.exception(
                    "Failed to parse generation Codex runner logs for task `%s`.",
                    task_id,
                )
        return GenerationCodexRunResult(
            analysis_markdown=analysis_markdown,
            runner_container_name=getattr(runner_container, "name", None),
            codex_session_id=parsed_logs.codex_session_id,
            codex_model=parsed_logs.codex_model,
            steps=parsed_logs.steps,
        )
    finally:
        _remove_container(runner_container)
        _remove_container(copy_container)
        _remove_volume(workspace_volume)
        client.close()
