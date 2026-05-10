from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

SOURCE_DIRNAME = "источники"
ANALYTICS_DIRNAME = "аналитика"
DOCUMENTS_DIRNAME = "документы"
DEFAULT_STAGE_DIR = Path("/tmp/analyze")
COMPANION_SUFFIXES = ("_images", "_files")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copy_benchmark.py",
        description=(
            "Собирает benchmark-папки по bench_info.json или staging'ит один период "
            "в плоский рабочий каталог."
        ),
    )
    parser.add_argument(
        "dataset_root",
        help="Путь к корню датасета, где лежат bench_info.json, аналитика/ и источники/.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Куда собирать benchmark-папки. По умолчанию: <dataset_root>/benchmark. "
            "Игнорируется в режиме --stage-period."
        ),
    )
    parser.add_argument(
        "-p",
        "--stage-period",
        help=(
            "Если указан, копирует только этот период в один каталог для ручного прогона "
            "вместо сборки всех benchmark-папок."
        ),
    )
    parser.add_argument(
        "-s",
        "--stage-dir",
        default=str(DEFAULT_STAGE_DIR),
        help="Каталог для staging-режима. По умолчанию: /tmp/analyze.",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_dataset_root(raw_path: str) -> Path:
    dataset_root = Path(raw_path).expanduser().resolve()
    if not dataset_root.exists():
        fail(f"dataset root not found: {dataset_root}")
    if not dataset_root.is_dir():
        fail(f"dataset root is not a directory: {dataset_root}")
    return dataset_root


def normalize_reference_paths(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    fail("reference_analysis_path must be a string, a list of strings, or null")
    return []


def load_bench_info(dataset_root: Path) -> dict[str, dict[str, object]]:
    bench_info_path = dataset_root / "bench_info.json"
    if not bench_info_path.is_file():
        fail(f"bench_info.json not found: {bench_info_path}")

    with bench_info_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        fail("bench_info.json must contain a JSON object at the top level")

    normalized: dict[str, dict[str, object]] = {}
    numeric_keys: list[int] = []

    for key, value in data.items():
        if not isinstance(key, str) or not key.isdigit():
            fail("all top-level keys in bench_info.json must be numeric strings")
        if not isinstance(value, dict):
            fail(f"bench_info entry {key!r} must be a JSON object")

        analytics = value.get("analytics")
        sources = value.get("sources")
        if not isinstance(analytics, list) or not all(isinstance(item, str) for item in analytics):
            fail(f"entry {key!r}: analytics must be a list of strings")
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            fail(f"entry {key!r}: sources must be a list of strings")

        normalized[key] = {
            "analytics": analytics,
            "sources": sources,
            "generated_analysis_period_description": value.get("generated_analysis_period_description", ""),
            "generated_analysis_name": value.get("generated_analysis_name", ""),
            "reference_analysis_path": normalize_reference_paths(value.get("reference_analysis_path")),
        }
        numeric_keys.append(int(key))

    if not numeric_keys:
        fail("bench_info.json is empty")

    sorted_keys = sorted(numeric_keys)
    expected_keys = list(range(2, sorted_keys[-1] + 1))
    if sorted_keys != expected_keys:
        fail(
            "bench_info.json must contain contiguous numeric keys starting from 2; "
            f"got {sorted_keys}"
        )

    return {str(key): normalized[str(key)] for key in sorted_keys}


def ensure_relative_path(rel_path: str, expected_prefix: str) -> Path:
    path = Path(rel_path)
    if path.is_absolute():
        fail(f"path must be relative, got: {rel_path}")
    if any(part == ".." for part in path.parts):
        fail(f"path must not contain '..', got: {rel_path}")
    if not path.parts or path.parts[0] != expected_prefix:
        fail(f"path must start with '{expected_prefix}/', got: {rel_path}")
    return path


def iter_companion_directories(source_file: Path) -> Iterable[Path]:
    stem = source_file.stem
    for suffix in COMPANION_SUFFIXES:
        candidate = source_file.parent / f"{stem}{suffix}"
        if candidate.is_dir():
            yield candidate


def copy_path(source_root: Path, relative_path: Path, target_root: Path) -> None:
    if len(relative_path.parts) < 2:
        fail(f"unexpected relative path shape: {relative_path}")

    source_path = source_root / relative_path
    if not source_path.exists():
        fail(f"missing source file: {source_path}")

    target_path = target_root / Path(*relative_path.parts[1:])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if source_path.is_dir():
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)
        return

    shutil.copy2(source_path, target_path)

    for companion_dir in iter_companion_directories(source_path):
        companion_target = target_path.parent / companion_dir.name
        shutil.copytree(companion_dir, companion_target, dirs_exist_ok=True)


def clean_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def unique_paths(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def populate_period(
    dataset_root: Path,
    entry: dict[str, object],
    analytics_dir: Path,
    documents_dir: Path,
) -> tuple[int, int]:
    analytics = unique_paths(entry["analytics"])  # type: ignore[arg-type]
    sources = unique_paths(entry["sources"])  # type: ignore[arg-type]

    for analytics_path in analytics:
        copy_path(
            dataset_root,
            ensure_relative_path(analytics_path, ANALYTICS_DIRNAME),
            analytics_dir,
        )

    for source_path in sources:
        copy_path(
            dataset_root,
            ensure_relative_path(source_path, SOURCE_DIRNAME),
            documents_dir,
        )

    return len(analytics), len(sources)


def build_benchmark(dataset_root: Path, bench_info: dict[str, dict[str, object]], output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "bench_info.json", bench_info)
    print(f"building benchmark at {output_root}")

    for period, entry in bench_info.items():
        period_dir = output_root / period
        analytics_dir = period_dir / ANALYTICS_DIRNAME
        documents_dir = period_dir / DOCUMENTS_DIRNAME

        clean_directory(analytics_dir)
        clean_directory(documents_dir)

        analytics_count, sources_count = populate_period(
            dataset_root=dataset_root,
            entry=entry,
            analytics_dir=analytics_dir,
            documents_dir=documents_dir,
        )
        stale_metadata_path = period_dir / "metadata.json"
        if stale_metadata_path.exists():
            stale_metadata_path.unlink()

        print(
            f"[{period}] analytics={analytics_count} "
            f"documents={sources_count} -> {period_dir}"
        )


def stage_single_period(
    dataset_root: Path,
    bench_info: dict[str, dict[str, object]],
    period: str,
    stage_dir: Path,
) -> tuple[int, int]:
    if period not in bench_info:
        fail(f"period {period!r} not found in bench_info.json")

    clean_directory(stage_dir)
    analytics_dir = stage_dir / ANALYTICS_DIRNAME
    documents_dir = stage_dir / DOCUMENTS_DIRNAME

    analytics_dir.mkdir(parents=True, exist_ok=True)
    documents_dir.mkdir(parents=True, exist_ok=True)

    entry = bench_info[period]
    analytics_count, sources_count = populate_period(
        dataset_root=dataset_root,
        entry=entry,
        analytics_dir=analytics_dir,
        documents_dir=documents_dir,
    )

    print(
        f"staged period {period} at {stage_dir} "
        f"(analytics={analytics_count}, documents={sources_count})"
    )
    return analytics_count, sources_count


def main() -> None:
    args = parse_args()
    dataset_root = resolve_dataset_root(args.dataset_root)
    bench_info = load_bench_info(dataset_root)

    if args.stage_period is not None:
        stage_single_period(
            dataset_root=dataset_root,
            bench_info=bench_info,
            period=str(args.stage_period),
            stage_dir=Path(args.stage_dir).expanduser().resolve(),
        )
        return

    output_root = Path(args.output).expanduser().resolve() if args.output else dataset_root / "benchmark"
    build_benchmark(
        dataset_root=dataset_root,
        bench_info=bench_info,
        output_root=output_root,
    )


if __name__ == "__main__":
    main()
