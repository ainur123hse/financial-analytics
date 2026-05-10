from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any

from dotenv import load_dotenv

from execute_benchmark.copy_benchmark import (
    ANALYTICS_DIRNAME,
    DOCUMENTS_DIRNAME,
    clean_directory,
    copy_path,
    ensure_relative_path,
    load_bench_info,
    populate_period,
    resolve_dataset_root,
)

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_STAGE_ROOT = Path("/tmp/analyze")
MAX_PERIODS = 4
TMUX_SESSION_BASE = "codex-benchmark"
TMUX_PASSTHROUGH_ENV_KEYS = ("OPENROUTER_API_KEY", "LLM_API_KEY")
REPO_ROOT = Path(__file__).resolve().parent.parent
EVALUATION_SCHEMA_PATH = Path(__file__).with_name("evaluation.schema.json")


def load_runtime_env() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    if not os.environ.get("OPENROUTER_API_KEY") and os.environ.get("LLM_API_KEY"):
        os.environ["OPENROUTER_API_KEY"] = os.environ["LLM_API_KEY"]


load_runtime_env()


@dataclass(frozen=True)
class BenchmarkEntry:
    analytics: list[str]
    sources: list[str]
    generated_analysis_period_description: str
    generated_analysis_name: str
    reference_analysis_paths: list[str]


@dataclass
class PeriodRunResult:
    period: str
    status: str
    generated_analysis_name: str
    reference_analysis_paths: list[str]
    generated_analysis_path: str | None
    evaluation_path: str | None
    generation_stdout_log: str
    generation_stderr_log: str
    evaluation_stdout_log: str | None
    evaluation_stderr_log: str | None
    reference_claims_count: int | None = None
    generated_claims_count: int | None = None
    matched_claims_count: int | None = None
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    comment: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_benchmark.py",
        description=(
            "Prepares up to four benchmark periods, opens them in tmux panes, "
            "and runs generation/evaluation through the interactive Codex CLI."
        ),
    )
    parser.add_argument(
        "dataset_root",
        nargs="?",
        help="Path to the dataset root with bench_info.json, аналитика/ and источники/.",
    )
    parser.add_argument(
        "--period",
        action="append",
        default=[],
        help="Benchmark period to run. Repeat the flag to run multiple periods.",
    )
    parser.add_argument(
        "--results-root",
        help="Where to store benchmark_results. Default: <dataset_root>/benchmark_results.",
    )
    parser.add_argument(
        "--stage-root",
        default=str(DEFAULT_STAGE_ROOT),
        help="Temporary root for staged benchmark folders. Default: /tmp/analyze.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name passed to the Codex CLI. Default: {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI binary or stub path. Default: codex.",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Create the tmux session without attaching to it.",
    )
    parser.add_argument(
        "--keep-stage",
        action="store_true",
        help="Keep staged generation/evaluation folders after the run finishes.",
    )
    parser.add_argument("--worker-manifest", help=argparse.SUPPRESS)
    parser.add_argument("--worker-period", help=argparse.SUPPRESS)
    return parser.parse_args()


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_text(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def validate_generated_analysis_name(period: str, filename: str) -> str:
    value = filename.strip()
    if not value:
        fail(f"period {period}: generated_analysis_name must not be empty")
    path = Path(value)
    if path.name != value or len(path.parts) != 1:
        fail(
            f"period {period}: generated_analysis_name must be a single filename, got {filename!r}"
        )
    return value


def normalize_entry(period: str, raw_entry: dict[str, object]) -> BenchmarkEntry:
    generated_analysis_period_description = str(
        raw_entry.get("generated_analysis_period_description", "")
    ).strip()
    if not generated_analysis_period_description:
        fail(f"period {period}: generated_analysis_period_description must not be empty")

    generated_analysis_name = validate_generated_analysis_name(
        period=period,
        filename=str(raw_entry.get("generated_analysis_name", "")),
    )

    raw_reference_paths = raw_entry.get("reference_analysis_path")
    if not isinstance(raw_reference_paths, list) or not raw_reference_paths:
        fail(f"period {period}: reference_analysis_path must be a non-empty list")

    reference_analysis_paths: list[str] = []
    seen_reference_paths: set[str] = set()
    for reference_path in raw_reference_paths:
        if not isinstance(reference_path, str):
            fail(f"period {period}: reference_analysis_path must contain only strings")
        normalized_reference_path = str(
            ensure_relative_path(reference_path, ANALYTICS_DIRNAME)
        )
        if normalized_reference_path not in seen_reference_paths:
            seen_reference_paths.add(normalized_reference_path)
            reference_analysis_paths.append(normalized_reference_path)

    analytics = raw_entry.get("analytics")
    sources = raw_entry.get("sources")
    if not isinstance(analytics, list) or not all(isinstance(item, str) for item in analytics):
        fail(f"period {period}: analytics must be a list of strings")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        fail(f"period {period}: sources must be a list of strings")

    return BenchmarkEntry(
        analytics=list(analytics),
        sources=list(sources),
        generated_analysis_period_description=generated_analysis_period_description,
        generated_analysis_name=generated_analysis_name,
        reference_analysis_paths=reference_analysis_paths,
    )


def normalize_bench_info(
    raw_bench_info: dict[str, dict[str, object]]
) -> dict[str, BenchmarkEntry]:
    return {
        period: normalize_entry(period=period, raw_entry=raw_entry)
        for period, raw_entry in raw_bench_info.items()
    }


def deserialize_entry(period: str, raw_entry: dict[str, object]) -> BenchmarkEntry:
    analytics = raw_entry.get("analytics")
    sources = raw_entry.get("sources")
    reference_analysis_paths = raw_entry.get("reference_analysis_paths")
    generated_analysis_period_description = str(
        raw_entry.get("generated_analysis_period_description", "")
    ).strip()

    if not generated_analysis_period_description:
        fail(f"period {period}: generated_analysis_period_description must not be empty")
    if not isinstance(analytics, list) or not all(isinstance(item, str) for item in analytics):
        fail(f"period {period}: analytics must be a list of strings")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        fail(f"period {period}: sources must be a list of strings")
    if not isinstance(reference_analysis_paths, list) or not all(
        isinstance(item, str) for item in reference_analysis_paths
    ):
        fail(f"period {period}: reference_analysis_paths must be a list of strings")

    return BenchmarkEntry(
        analytics=list(analytics),
        sources=list(sources),
        generated_analysis_period_description=generated_analysis_period_description,
        generated_analysis_name=validate_generated_analysis_name(
            period=period,
            filename=str(raw_entry.get("generated_analysis_name", "")),
        ),
        reference_analysis_paths=list(reference_analysis_paths),
    )


def select_periods(
    bench_info: dict[str, BenchmarkEntry],
    requested_periods: list[str],
) -> list[str]:
    if not requested_periods:
        return list(bench_info.keys())

    selected_periods: list[str] = []
    seen_periods: set[str] = set()
    for raw_period in requested_periods:
        period = str(raw_period).strip()
        if not period:
            fail("period value must not be empty")
        if period not in bench_info:
            fail(f"period {period!r} not found in bench_info.json")
        if period in seen_periods:
            continue
        seen_periods.add(period)
        selected_periods.append(period)

    return selected_periods


def ensure_period_limit(periods: list[str]) -> None:
    if len(periods) > MAX_PERIODS:
        fail(
            f"selected {len(periods)} periods, but this workflow supports at most {MAX_PERIODS} "
            "periods per tmux run"
        )


def ensure_run_dir(results_root: Path) -> tuple[str, Path]:
    run_id = make_run_id()
    run_dir = results_root / run_id
    if run_dir.exists():
        fail(f"results directory already exists for run_id {run_id}: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def resolve_results_root(dataset_root: Path, raw_results_root: str | None) -> Path:
    if raw_results_root is None:
        return dataset_root / "benchmark_results"
    return Path(raw_results_root).expanduser().resolve()


def relative_to_run_dir(path: Path, run_dir: Path) -> str:
    return str(path.relative_to(run_dir))


def period_run_dir(run_dir: Path, period: str) -> Path:
    return run_dir / period


def generation_stage_dir(stage_run_dir: Path, period: str) -> Path:
    return stage_run_dir / period / "generation"


def evaluation_stage_dir(stage_run_dir: Path, period: str) -> Path:
    return stage_run_dir / period / "evaluation"


def generation_prompt_path(run_dir: Path, period: str) -> Path:
    return period_run_dir(run_dir, period) / "generation.prompt.txt"


def evaluation_prompt_path(run_dir: Path, period: str) -> Path:
    return period_run_dir(run_dir, period) / "evaluation.prompt.txt"


def period_result_path(run_dir: Path, period: str) -> Path:
    return period_run_dir(run_dir, period) / "result.json"


def stage_generation_period(
    dataset_root: Path,
    entry: BenchmarkEntry,
    generation_dir: Path,
) -> tuple[int, int]:
    clean_directory(generation_dir)
    analytics_dir = generation_dir / ANALYTICS_DIRNAME
    documents_dir = generation_dir / DOCUMENTS_DIRNAME
    analytics_dir.mkdir(parents=True, exist_ok=True)
    documents_dir.mkdir(parents=True, exist_ok=True)

    return populate_period(
        dataset_root=dataset_root,
        entry={
            "analytics": entry.analytics,
            "sources": entry.sources,
        },
        analytics_dir=analytics_dir,
        documents_dir=documents_dir,
    )


def stage_evaluation_period(
    dataset_root: Path,
    generated_file: Path,
    entry: BenchmarkEntry,
    evaluation_dir: Path,
) -> None:
    clean_directory(evaluation_dir)
    candidate_dir = evaluation_dir / "candidate"
    reference_dir = evaluation_dir / "reference"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(generated_file, candidate_dir / generated_file.name)
    shutil.copy2(EVALUATION_SCHEMA_PATH, evaluation_dir / EVALUATION_SCHEMA_PATH.name)

    for reference_path in entry.reference_analysis_paths:
        copy_path(
            dataset_root,
            ensure_relative_path(reference_path, ANALYTICS_DIRNAME),
            reference_dir,
        )


def build_generation_prompt(entry: BenchmarkEntry) -> str:
    return dedent(
        f"""
        На основе аналитики за прошлые периоды `{ANALYTICS_DIRNAME}/`
        и документов-источников из `{DOCUMENTS_DIRNAME}/`.

        На этой базе сформируй аналитику по компании за новый целевой период:
        {entry.generated_analysis_period_description}.

        Требования:
        - В первую очередь опирайся на информацию из файлов, которые есть в текущей рабочей папке.
        - Можешь дополнительно искать недостающую информацию в интернете, но при этом не опирайся на неофициальные оценочные мнения за исключением тех что есть в `{ANALYTICS_DIRNAME}/`

        Что сделать:
        - Сохрани итоговый Markdown-документ в файл `{entry.generated_analysis_name}` в текущей рабочей папке.
        - Если файл уже существует, полностью перезапиши его.
        - После сохранения файла коротко сообщи, что файл готов.
        """
    ).strip()


def build_evaluation_prompt(entry: BenchmarkEntry) -> str:
    return dedent(
        f"""
        Сравни сгенерированную аналитику из `candidate/` с эталонной аналитикой из `reference/`.
        В текущей рабочей папке также лежит JSON Schema файл `evaluation.schema.json`.

        Правила сравнения:
        - Эталонные документы из `reference/` считаются источником истины для оценки периода.
        - Считай claim-level покрытие: отдельные содержательные тезисы, факты, выводы и оценки.
        - Засчитывай claim как matched, если смысл восстановлен корректно, даже если формулировка отличается.
        - Не засчитывай claim как matched, если в candidate он искажен, подменен более слабым тезисом или фактически отсутствует.
        - В `missing_reference_claims` перечисли только ключевые тезисы из reference, которых не хватает в candidate.
        - В `unsupported_generated_claims` перечисли только ключевые тезисы из candidate, которые не подтверждаются reference.
        - Учитывай все документы из `reference/`, включая дополнительные материалы того же периода.

        Что должно быть в JSON:
        - `reference_claims_count`: число уникальных существенных claim'ов в эталонных документах.
        - `generated_claims_count`: число уникальных существенных claim'ов в candidate.
        - `matched_claims_count`: число эталонных claim'ов, которые candidate восстановил корректно.
        - `precision`: matched_claims_count / generated_claims_count, либо 0 если generated_claims_count = 0.
        - `recall`: matched_claims_count / reference_claims_count, либо 0 если reference_claims_count = 0.
        - `missing_reference_claims`: список ключевых тезисов из reference, которых не хватает в candidate.
        - `unsupported_generated_claims`: список ключевых тезисов из candidate, которые не подтверждаются reference.
        - `comment`: короткий вывод по качеству периода.

        Контекст периода:
        - generated_analysis_name: `{entry.generated_analysis_name}`
        - target period: `{entry.generated_analysis_period_description}`

        Что сделать:
        - Сформируй JSON, который соответствует `evaluation.schema.json`.
        - Сохрани его в файл `evaluation.raw.json` в текущей рабочей папке.
        - Если файл уже существует, полностью перезапиши его.
        - Не создавай дополнительных итоговых файлов вместо `evaluation.raw.json`.
        - После сохранения файла коротко сообщи, что файл готов.
        """
    ).strip()


def require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def require_ratio(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    value = float(value)
    if value < 0 or value > 1:
        raise ValueError(f"{key} must be between 0 and 1")
    return value


def require_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def validate_evaluation_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("evaluation payload must be a JSON object")

    reference_claims_count = require_int(payload, "reference_claims_count")
    generated_claims_count = require_int(payload, "generated_claims_count")
    matched_claims_count = require_int(payload, "matched_claims_count")
    if matched_claims_count > reference_claims_count:
        raise ValueError("matched_claims_count cannot exceed reference_claims_count")
    if matched_claims_count > generated_claims_count:
        raise ValueError("matched_claims_count cannot exceed generated_claims_count")

    missing_reference_claims = require_string_list(payload, "missing_reference_claims")
    unsupported_generated_claims = require_string_list(payload, "unsupported_generated_claims")

    comment = payload.get("comment")
    if not isinstance(comment, str):
        raise ValueError("comment must be a string")

    return {
        "reference_claims_count": reference_claims_count,
        "generated_claims_count": generated_claims_count,
        "matched_claims_count": matched_claims_count,
        "precision": require_ratio(payload, "precision"),
        "recall": require_ratio(payload, "recall"),
        "missing_reference_claims": missing_reference_claims,
        "unsupported_generated_claims": unsupported_generated_claims,
        "comment": comment,
    }


def compute_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def compute_f1(precision: float, recall: float) -> float:
    if precision == 0 and recall == 0:
        return 0.0
    return round((2 * precision * recall) / (precision + recall), 6)


def compute_macro_average(results: list[PeriodRunResult]) -> dict[str, Any]:
    successful_results = [result for result in results if result.status == "completed"]
    if not successful_results:
        return {
            "successful_periods": 0,
            "precision": None,
            "recall": None,
            "f1": None,
        }

    precision = round(
        sum(result.precision or 0.0 for result in successful_results)
        / len(successful_results),
        6,
    )
    recall = round(
        sum(result.recall or 0.0 for result in successful_results)
        / len(successful_results),
        6,
    )
    f1 = round(
        sum(result.f1 or 0.0 for result in successful_results) / len(successful_results),
        6,
    )
    return {
        "successful_periods": len(successful_results),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def format_metric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def build_summary_markdown(
    *,
    run_id: str,
    dataset_root: Path,
    model: str,
    results: list[PeriodRunResult],
    macro_average: dict[str, Any],
) -> str:
    lines = [
        f"# Benchmark Run `{run_id}`",
        "",
        f"- Dataset: `{dataset_root}`",
        f"- Model: `{model}`",
        f"- Successful periods: `{macro_average['successful_periods']}`",
        "",
        "| Period | Status | Precision | Recall | F1 | Generated | Evaluation |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    result.period,
                    result.status,
                    format_metric(result.precision),
                    format_metric(result.recall),
                    format_metric(result.f1),
                    result.generated_analysis_path or "n/a",
                    result.evaluation_path or "n/a",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Macro Average",
            "",
            "| Precision | Recall | F1 |",
            "| ---: | ---: | ---: |",
            "| "
            + " | ".join(
                [
                    format_metric(macro_average["precision"]),
                    format_metric(macro_average["recall"]),
                    format_metric(macro_average["f1"]),
                ]
            )
            + " |",
        ]
    )

    return "\n".join(lines) + "\n"


def resolve_executable(raw_path: str, *, name: str) -> str:
    expanded = Path(raw_path).expanduser()
    if "/" in raw_path:
        resolved = expanded.resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            fail(f"{name} is not executable: {resolved}")
        return str(resolved)

    resolved = shutil.which(raw_path)
    if resolved is None:
        fail(f"{name} not found in PATH: {raw_path}")
    return resolved


def resolve_tmux_bin() -> str:
    return resolve_executable("tmux", name="tmux")


def build_command_preview(cmd: list[str], prompt_path: Path) -> str:
    preview_cmd = [*cmd[:-1], f"<prompt from {prompt_path.name}>"]
    return shlex.join(preview_cmd)


def write_interactive_log_header(
    *,
    stdout_log: Path,
    stderr_log: Path,
    working_dir: Path,
    prompt_path: Path,
    output_file: Path,
    cmd: list[str],
) -> None:
    header = (
        "Interactive CLI mode.\n"
        f"Working directory: {working_dir}\n"
        f"Prompt file: {prompt_path}\n"
        f"Expected output file in working directory: {output_file.name}\n"
        f"Launched command: {build_command_preview(cmd, prompt_path)}\n"
        "Terminal transcript is not captured in this log.\n"
        "Exit the Codex session after it reports that the file is ready.\n"
    )
    stdout_log.write_text(header, encoding="utf-8")
    stderr_log.write_text(header, encoding="utf-8")


def append_exit_code_to_logs(
    *,
    stdout_log: Path,
    stderr_log: Path,
    returncode: int,
) -> None:
    line = f"Codex CLI exit code: {returncode}\n"
    append_text(stdout_log, line)
    append_text(stderr_log, line)


def run_codex_cli_interactive(
    *,
    codex_bin: str,
    model: str,
    working_dir: Path,
    prompt_path: Path,
    output_file: Path,
    stdout_log: Path,
    stderr_log: Path,
) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    cmd = [
        codex_bin,
        "--full-auto",
        "--no-alt-screen",
        "-C",
        str(working_dir),
        "-m",
        model,
        prompt,
    ]

    write_interactive_log_header(
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        working_dir=working_dir,
        prompt_path=prompt_path,
        output_file=output_file,
        cmd=cmd,
    )

    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        completed = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        append_text(stderr_log, str(exc) + "\n")
        raise RuntimeError(f"failed to start Codex CLI: {exc}") from exc
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)

    append_exit_code_to_logs(
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        returncode=completed.returncode,
    )

    if completed.returncode not in (0, 130):
        raise RuntimeError(
            f"Codex CLI exited with code {completed.returncode}; see {stderr_log.name}"
        )
    if not output_file.is_file():
        raise RuntimeError(f"Codex CLI did not create output file: {output_file}")
    if not output_file.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"Codex CLI created an empty output file: {output_file}")


def prepare_period_environment(
    *,
    dataset_root: Path,
    period: str,
    entry: BenchmarkEntry,
    run_dir: Path,
    stage_run_dir: Path,
) -> None:
    period_dir = period_run_dir(run_dir, period)
    period_dir.mkdir(parents=True, exist_ok=True)
    (period_dir / "generated").mkdir(parents=True, exist_ok=True)

    stage_generation_period(
        dataset_root=dataset_root,
        entry=entry,
        generation_dir=generation_stage_dir(stage_run_dir, period),
    )

    generation_prompt_path(run_dir, period).write_text(
        build_generation_prompt(entry),
        encoding="utf-8",
    )
    evaluation_prompt_path(run_dir, period).write_text(
        build_evaluation_prompt(entry),
        encoding="utf-8",
    )


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        fail(f"manifest must be a JSON object: {manifest_path}")
    return payload


def write_period_result(run_dir: Path, result: PeriodRunResult) -> None:
    write_json(period_result_path(run_dir, result.period), result.to_dict())


def maybe_finalize_run(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    run_dir = Path(str(manifest["run_dir"]))
    dataset_root = Path(str(manifest["dataset_root"]))
    stage_run_dir = Path(str(manifest["stage_run_dir"]))
    periods = [str(period) for period in manifest["periods"]]

    result_paths = [period_result_path(run_dir, period) for period in periods]
    if not all(path.is_file() for path in result_paths):
        return

    results = [
        PeriodRunResult(**json.loads(path.read_text(encoding="utf-8")))
        for path in result_paths
    ]
    macro_average = compute_macro_average(results)
    summary_payload = {
        "run_id": str(manifest["run_id"]),
        "dataset_root": str(dataset_root),
        "results_root": str(run_dir),
        "model": str(manifest["model"]),
        "periods": [result.to_dict() for result in results],
        "macro_average": macro_average,
        "started_at_utc": str(manifest["started_at_utc"]),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(run_dir / "summary.json", summary_payload)
    (run_dir / "summary.md").write_text(
        build_summary_markdown(
            run_id=str(manifest["run_id"]),
            dataset_root=dataset_root,
            model=str(manifest["model"]),
            results=results,
            macro_average=macro_average,
        ),
        encoding="utf-8",
    )

    if not bool(manifest.get("keep_stage", False)):
        shutil.rmtree(stage_run_dir, ignore_errors=True)


def run_period_worker(manifest_path: Path, period: str) -> int:
    manifest = load_manifest(manifest_path)
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or period not in entries:
        fail(f"period {period!r} not found in worker manifest")
    raw_entry = entries[period]
    if not isinstance(raw_entry, dict):
        fail(f"period {period!r} entry in worker manifest must be an object")

    dataset_root = Path(str(manifest["dataset_root"]))
    run_dir = Path(str(manifest["run_dir"]))
    stage_run_dir = Path(str(manifest["stage_run_dir"]))
    entry = deserialize_entry(period=period, raw_entry=raw_entry)
    period_dir = period_run_dir(run_dir, period)
    generation_dir = generation_stage_dir(stage_run_dir, period)
    evaluation_dir = evaluation_stage_dir(stage_run_dir, period)

    staged_generated_file = generation_dir / entry.generated_analysis_name
    published_generated_file = period_dir / "generated" / entry.generated_analysis_name
    raw_evaluation_output = evaluation_dir / "evaluation.raw.json"

    generation_stdout_log = period_dir / "generation.stdout.log"
    generation_stderr_log = period_dir / "generation.stderr.log"
    evaluation_stdout_log = period_dir / "evaluation.stdout.log"
    evaluation_stderr_log = period_dir / "evaluation.stderr.log"

    result = PeriodRunResult(
        period=period,
        status="failed",
        generated_analysis_name=entry.generated_analysis_name,
        reference_analysis_paths=list(entry.reference_analysis_paths),
        generated_analysis_path=None,
        evaluation_path=None,
        generation_stdout_log=relative_to_run_dir(generation_stdout_log, run_dir),
        generation_stderr_log=relative_to_run_dir(generation_stderr_log, run_dir),
        evaluation_stdout_log=None,
        evaluation_stderr_log=None,
    )

    try:
        print(f"[{period}] generation started in {generation_dir}")
        run_codex_cli_interactive(
            codex_bin=str(manifest["codex_bin"]),
            model=str(manifest["model"]),
            working_dir=generation_dir,
            prompt_path=generation_prompt_path(run_dir, period),
            output_file=staged_generated_file,
            stdout_log=generation_stdout_log,
            stderr_log=generation_stderr_log,
        )
        shutil.copy2(staged_generated_file, published_generated_file)
        result.generated_analysis_path = relative_to_run_dir(published_generated_file, run_dir)
    except Exception as exc:
        result.error = f"generation failed: {exc}"
        write_period_result(run_dir, result)
        maybe_finalize_run(manifest_path)
        print(f"[{period}] failed: {result.error}")
        return 1

    try:
        stage_evaluation_period(
            dataset_root=dataset_root,
            generated_file=staged_generated_file,
            entry=entry,
            evaluation_dir=evaluation_dir,
        )
        result.evaluation_stdout_log = relative_to_run_dir(evaluation_stdout_log, run_dir)
        result.evaluation_stderr_log = relative_to_run_dir(evaluation_stderr_log, run_dir)

        print(f"[{period}] evaluation started in {evaluation_dir}")
        run_codex_cli_interactive(
            codex_bin=str(manifest["codex_bin"]),
            model=str(manifest["model"]),
            working_dir=evaluation_dir,
            prompt_path=evaluation_prompt_path(run_dir, period),
            output_file=raw_evaluation_output,
            stdout_log=evaluation_stdout_log,
            stderr_log=evaluation_stderr_log,
        )

        evaluator_payload = validate_evaluation_payload(
            json.loads(raw_evaluation_output.read_text(encoding="utf-8"))
        )
        precision = compute_ratio(
            evaluator_payload["matched_claims_count"],
            evaluator_payload["generated_claims_count"],
        )
        recall = compute_ratio(
            evaluator_payload["matched_claims_count"],
            evaluator_payload["reference_claims_count"],
        )
        f1 = compute_f1(precision=precision, recall=recall)

        evaluation_payload = {
            "period": period,
            "generated_analysis_name": entry.generated_analysis_name,
            "reference_analysis_paths": list(entry.reference_analysis_paths),
            "reference_claims_count": evaluator_payload["reference_claims_count"],
            "generated_claims_count": evaluator_payload["generated_claims_count"],
            "matched_claims_count": evaluator_payload["matched_claims_count"],
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "missing_reference_claims": evaluator_payload["missing_reference_claims"],
            "unsupported_generated_claims": evaluator_payload["unsupported_generated_claims"],
            "comment": evaluator_payload["comment"],
        }

        evaluation_path = period_dir / "evaluation.json"
        write_json(evaluation_path, evaluation_payload)
        raw_evaluation_output.unlink(missing_ok=True)

        result.status = "completed"
        result.evaluation_path = relative_to_run_dir(evaluation_path, run_dir)
        result.reference_claims_count = evaluation_payload["reference_claims_count"]
        result.generated_claims_count = evaluation_payload["generated_claims_count"]
        result.matched_claims_count = evaluation_payload["matched_claims_count"]
        result.precision = evaluation_payload["precision"]
        result.recall = evaluation_payload["recall"]
        result.f1 = evaluation_payload["f1"]
        result.comment = evaluation_payload["comment"]
    except Exception as exc:
        result.error = f"evaluation failed: {exc}"

    write_period_result(run_dir, result)
    maybe_finalize_run(manifest_path)

    if result.status == "completed":
        print(
            f"[{period}] completed "
            f"precision={format_metric(result.precision)} "
            f"recall={format_metric(result.recall)} "
            f"f1={format_metric(result.f1)}"
        )
        return 0

    print(f"[{period}] failed: {result.error}")
    return 1


def tmux_run(tmux_bin: str, *args: str, capture_output: bool = False) -> str:
    completed = subprocess.run(
        [tmux_bin, *args],
        check=True,
        text=True,
        capture_output=capture_output,
    )
    if capture_output:
        return completed.stdout.strip()
    return ""


def next_tmux_session_name(tmux_bin: str, base_name: str) -> str:
    session_name = base_name
    suffix = 1

    while subprocess.run(
        [tmux_bin, "has-session", "-t", session_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0:
        session_name = f"{base_name}-{suffix}"
        suffix += 1

    return session_name


def tmux_environment_args() -> list[str]:
    args: list[str] = []
    for key in TMUX_PASSTHROUGH_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            args.extend(["-e", f"{key}={value}"])
    return args


def configure_tmux_window(tmux_bin: str, window_target: str) -> None:
    tmux_run(tmux_bin, "set-window-option", "-t", window_target, "remain-on-exit", "on")
    tmux_run(tmux_bin, "set-window-option", "-t", window_target, "pane-border-status", "top")
    tmux_run(
        tmux_bin,
        "set-window-option",
        "-t",
        window_target,
        "pane-border-format",
        "#{pane_title}",
    )


def build_worker_command(manifest_path: Path, period: str) -> str:
    cmd = [
        sys.executable,
        "-m",
        "execute_benchmark.run_benchmark",
        "--worker-manifest",
        str(manifest_path),
        "--worker-period",
        period,
    ]
    return "exec " + shlex.join(cmd)


def create_tmux_session(
    *,
    tmux_bin: str,
    manifest_path: Path,
    periods: list[str],
) -> str:
    tmux_env_args = tmux_environment_args()
    session_name = next_tmux_session_name(
        tmux_bin=tmux_bin,
        base_name=f"{TMUX_SESSION_BASE}-{manifest_path.parent.name}",
    )
    window_name = "benchmark"
    window_target = f"{session_name}:{window_name}"

    tmux_run(
        tmux_bin,
        "new-session",
        "-d",
        "-s",
        session_name,
        "-n",
        window_name,
        "-c",
        str(REPO_ROOT),
        *tmux_env_args,
    )
    configure_tmux_window(tmux_bin, window_target)

    pane_targets = [
        tmux_run(
            tmux_bin,
            "display-message",
            "-p",
            "-t",
            window_target,
            "#{pane_id}",
            capture_output=True,
        )
    ]

    for _ in periods[1:]:
        pane_targets.append(
            tmux_run(
                tmux_bin,
                "split-window",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                window_target,
                "-c",
                str(REPO_ROOT),
                *tmux_env_args,
                capture_output=True,
            )
        )
        tmux_run(tmux_bin, "select-layout", "-t", window_target, "tiled")

    for pane_target, period in zip(pane_targets, periods):
        tmux_run(tmux_bin, "select-pane", "-t", pane_target, "-T", f"period {period}")
        tmux_run(
            tmux_bin,
            "send-keys",
            "-t",
            pane_target,
            build_worker_command(manifest_path, period),
            "C-m",
        )

    return session_name


def attach_tmux_session(tmux_bin: str, session_name: str) -> None:
    if os.environ.get("TMUX"):
        os.execvp(tmux_bin, [tmux_bin, "switch-client", "-t", session_name])
    os.execvp(tmux_bin, [tmux_bin, "attach-session", "-t", session_name])


def run_orchestrator(args: argparse.Namespace) -> int:
    if not args.dataset_root:
        fail("dataset_root is required")
    if not EVALUATION_SCHEMA_PATH.is_file():
        fail(f"evaluation schema not found: {EVALUATION_SCHEMA_PATH}")

    dataset_root = resolve_dataset_root(args.dataset_root)
    results_root = resolve_results_root(dataset_root, args.results_root)
    results_root.mkdir(parents=True, exist_ok=True)

    raw_bench_info = load_bench_info(dataset_root)
    bench_info = normalize_bench_info(raw_bench_info)
    periods = select_periods(bench_info=bench_info, requested_periods=args.period)
    ensure_period_limit(periods)

    run_id, run_dir = ensure_run_dir(results_root)
    stage_root = Path(args.stage_root).expanduser().resolve()
    stage_run_dir = stage_root / run_id
    stage_run_dir.mkdir(parents=True, exist_ok=True)

    codex_bin = resolve_executable(args.codex_bin, name="Codex CLI")
    tmux_bin = resolve_tmux_bin()
    started_at = datetime.now(timezone.utc)

    for period in periods:
        prepare_period_environment(
            dataset_root=dataset_root,
            period=period,
            entry=bench_info[period],
            run_dir=run_dir,
            stage_run_dir=stage_run_dir,
        )

    manifest_path = run_dir / "run_manifest.json"
    write_json(
        manifest_path,
        {
            "run_id": run_id,
            "dataset_root": str(dataset_root),
            "run_dir": str(run_dir),
            "stage_run_dir": str(stage_run_dir),
            "model": args.model,
            "codex_bin": codex_bin,
            "keep_stage": args.keep_stage,
            "started_at_utc": started_at.isoformat(),
            "periods": periods,
            "entries": {period: asdict(bench_info[period]) for period in periods},
        },
    )

    session_name = create_tmux_session(
        tmux_bin=tmux_bin,
        manifest_path=manifest_path,
        periods=periods,
    )
    print(f"[execute_benchmark] created tmux session {session_name}")
    print(f"[execute_benchmark] run directory: {run_dir}")

    if args.detach:
        print(f"[execute_benchmark] detached; attach with: tmux attach -t {session_name}")
        return 0

    attach_tmux_session(tmux_bin, session_name)
    return 0


def main() -> None:
    args = parse_args()

    if args.worker_manifest or args.worker_period:
        if not args.worker_manifest or not args.worker_period:
            fail("--worker-manifest and --worker-period must be provided together")
        raise SystemExit(
            run_period_worker(
                manifest_path=Path(args.worker_manifest).expanduser().resolve(),
                period=args.worker_period,
            )
        )

    raise SystemExit(run_orchestrator(args))


if __name__ == "__main__":
    main()
