from __future__ import annotations

import re

WORKSPACE_DIR = "/workspace"
SOURCE_DIR = "/source"
RESULT_FILENAME = ".generation-result.json"
RESULT_PATH = f"{WORKSPACE_DIR}/{RESULT_FILENAME}"
GENERATED_ANALYSIS_FILENAME = ".generated-analysis.md"
GENERATED_ANALYSIS_PATH = f"{WORKSPACE_DIR}/{GENERATED_ANALYSIS_FILENAME}"
SCHEMA_PATH = "/app/app/codex_runner/generation_output_schema.json"
COPY_CONTAINER_PREFIX = "financial-analytics-generation-copy"
RUNNER_CONTAINER_PREFIX = "financial-analytics-generation-runner"
WORKSPACE_VOLUME_PREFIX = "financial-analytics-generation-workspace"
CONTAINER_POLL_INTERVAL_SECONDS = 1.0
MAX_LOG_CHARS = 8000
RUNNER_EXTRA_HOSTS = {"host.docker.internal": "host-gateway"}
EXEC_MARKER = "exec"
CODEX_MARKER = "codex"
USER_MARKER = "user"
TOKENS_USED_MARKER = "tokens used"
TIMESTAMPED_LOG_LINE_PATTERN = re.compile(r"^(?P<timestamp>\S+)(?:\s(?P<message>.*))?$")
RFC3339_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<prefix>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})$"
)
EXEC_STATUS_PATTERN = re.compile(
    r"^\s*(?:(?P<status>succeeded)|exited\s+(?P<exit_code>-?\d+))\s+in\s+"
    r"(?P<duration>[\d.]+)(?P<unit>ms|s):$"
)
EXEC_COMMAND_PATTERN = re.compile(r"^(?P<command>.+)\s+in\s+(?P<cwd>\S+)$")
REASONING_HEADING_PATTERN = re.compile(r"^\*\*(?P<title>.+?)\*\*$")
MARKER_MESSAGES = {EXEC_MARKER, CODEX_MARKER}
