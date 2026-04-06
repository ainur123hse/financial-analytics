import json
import mimetypes
import os
import shutil
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.observability import serialize_for_langfuse, start_observation, update_observation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = PROJECT_ROOT / ".agent_runs"
CODE_TIMEOUT_SECONDS = 90
DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 180

def execute_code(
    code: str,
    dependencies: list[str],
    run_dir: Path,
    iteration: int,
    timeout_seconds: int = CODE_TIMEOUT_SECONDS,
    install_timeout_seconds: int = DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    observation_input = serialize_for_langfuse(
        {
            "code": code,
            "dependencies": dependencies,
            "iteration": iteration,
            "run_dir": str(run_dir),
            "timeout_seconds": timeout_seconds,
            "install_timeout_seconds": install_timeout_seconds,
        }
    )

    with start_observation(
        name="tool.execute_code",
        as_type="tool",
        input=observation_input,
        metadata={"iteration": iteration},
    ) as observation:
        def finish(result: dict[str, Any]) -> dict[str, Any]:
            update_kwargs: dict[str, Any] = {"output": serialize_for_langfuse(result)}
            if not result["success"]:
                update_kwargs["level"] = "ERROR"
                update_kwargs["status_message"] = result["error"]
            update_observation(observation, **update_kwargs)
            return result

        try:
            run_dir = run_dir.resolve()
            (run_dir / "notes").mkdir(parents=True, exist_ok=True)
            (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

            script_path = run_dir / f"step_{iteration:02d}_generated.py"
            script_path.write_text(code, encoding="utf-8")

            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else str(PROJECT_ROOT)
            )
            env["PYTHONIOENCODING"] = "utf-8"

            dependency_install = _install_dependencies(
                dependencies=dependencies,
                run_dir=run_dir,
                env=env,
                timeout_seconds=install_timeout_seconds,
            )
            if not dependency_install["success"]:
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout="",
                        stderr="",
                        other_info="",
                        images_to_next_iteration=[],
                        error=dependency_install["error"],
                    )
                )

            try:
                completed = subprocess.run(
                    [sys.executable, script_path.name],
                    cwd=run_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout=stdout,
                        stderr=stderr,
                        other_info="",
                        images_to_next_iteration=[],
                        error=f"Code execution timed out after {timeout_seconds} seconds.",
                    )
                )

            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            if completed.returncode != 0:
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout=stdout,
                        stderr=stderr,
                        other_info="",
                        images_to_next_iteration=[],
                        error=f"Code exited with status {completed.returncode}.",
                    )
                )

            try:
                payload = json.loads(stdout.strip())
            except json.JSONDecodeError as exc:
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout=stdout,
                        stderr=stderr,
                        other_info="",
                        images_to_next_iteration=[],
                        error=f"stdout is not valid JSON: {exc.msg}.",
                    )
                )

            if not isinstance(payload, dict):
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout=stdout,
                        stderr=stderr,
                        other_info="",
                        images_to_next_iteration=[],
                        error="stdout JSON must be an object.",
                    )
                )

            try:
                if "images_to_next_iteration" not in payload:
                    raise ValueError('stdout JSON must include "images_to_next_iteration".')
                if "other_info" not in payload:
                    raise ValueError('stdout JSON must include "other_info".')

                other_info = payload["other_info"]
                if not isinstance(other_info, str):
                    raise ValueError('"other_info" must be a string.')

                images = _normalize_images(payload["images_to_next_iteration"], run_dir)
            except ValueError as exc:
                return finish(
                    _build_execution_result(
                        iteration=iteration,
                        success=False,
                        run_dir=run_dir,
                        script_path=script_path,
                        dependency_install=dependency_install,
                        stdout=stdout,
                        stderr=stderr,
                        other_info="",
                        images_to_next_iteration=[],
                        error=str(exc),
                    )
                )

            return finish(
                _build_execution_result(
                    iteration=iteration,
                    success=True,
                    run_dir=run_dir,
                    script_path=script_path,
                    dependency_install=dependency_install,
                    stdout=stdout,
                    stderr=stderr,
                    other_info=other_info.strip(),
                    images_to_next_iteration=images,
                    error=None,
                )
            )
        except Exception as exc:
            update_observation(
                observation,
                level="ERROR",
                status_message=str(exc),
            )
            raise


def _install_dependencies(
    dependencies: list[str],
    run_dir: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    requested_dependencies = [dependency.strip() for dependency in dependencies if dependency.strip()]
    if not requested_dependencies:
        return {
            "requested_dependencies": [],
            "status": "not_requested",
            "success": True,
            "stdout": "",
            "stderr": "",
            "error": None,
        }

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *requested_dependencies,
            ],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "requested_dependencies": requested_dependencies,
            "status": "error",
            "success": False,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": f"Dependency installation timed out after {timeout_seconds} seconds.",
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        return {
            "requested_dependencies": requested_dependencies,
            "status": "error",
            "success": False,
            "stdout": stdout,
            "stderr": stderr,
            "error": f"Dependency installation exited with status {completed.returncode}.",
        }

    return {
        "requested_dependencies": requested_dependencies,
        "status": "success",
        "success": True,
        "stdout": stdout,
        "stderr": stderr,
        "error": None,
    }


def _prepare_run_dir(path_to_pdf: Path) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    run_dir = RUNS_ROOT / run_id
    notes_dir = run_dir / "notes"
    artifacts_dir = run_dir / "artifacts"
    notes_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path_to_pdf, run_dir / "document.pdf")
    return run_dir


def _build_execution_result(
    *,
    iteration: int,
    success: bool,
    run_dir: Path,
    script_path: Path,
    dependency_install: dict[str, Any],
    stdout: str,
    stderr: str,
    other_info: str,
    images_to_next_iteration: list[dict[str, str]],
    error: str | None,
) -> dict[str, Any]:
    return {
        "iteration": iteration,
        "success": success,
        "status": "success" if success else "error",
        "script_path": _make_relative_to_run_dir(script_path, run_dir),
        "requested_dependencies": dependency_install["requested_dependencies"],
        "dependency_install_status": dependency_install["status"],
        "dependency_install_success": dependency_install["success"],
        "dependency_install_stdout": dependency_install["stdout"],
        "dependency_install_stderr": dependency_install["stderr"],
        "dependency_install_error": dependency_install["error"],
        "stdout": stdout,
        "stderr": stderr,
        "other_info": other_info,
        "images_to_next_iteration": images_to_next_iteration,
        "note_files": _collect_note_files(run_dir),
        "error": error,
    }


def _collect_note_files(run_dir: Path) -> list[str]:
    notes_dir = run_dir / "notes"
    if not notes_dir.exists():
        return []
    return sorted(
        _make_relative_to_run_dir(path, run_dir)
        for path in notes_dir.rglob("*.md")
        if path.is_file()
    )


def _make_relative_to_run_dir(path: Path, run_dir: Path) -> str:
    return str(path.resolve().relative_to(run_dir.resolve()))


def _normalize_images(images: Any, run_dir: Path) -> list[dict[str, str]]:
    if not isinstance(images, list):
        raise ValueError('"images_to_next_iteration" must be a list.')

    normalized: list[dict[str, str]] = []
    run_dir_resolved = run_dir.resolve()

    for index, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Image item #{index} must be an object.")

        image_path = item.get("image_path")
        image_info = item.get("image_info")
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError(f'Image item #{index} must include non-empty "image_path".')
        if not isinstance(image_info, str) or not image_info.strip():
            raise ValueError(f'Image item #{index} must include non-empty "image_info".')

        candidate = Path(image_path)
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        candidate = candidate.resolve()

        try:
            candidate.relative_to(run_dir_resolved)
        except ValueError as exc:
            raise ValueError(
                f'Image item #{index} points outside the run directory: "{image_path}".'
            ) from exc

        if not candidate.is_file():
            raise ValueError(f'Image item #{index} file not found: "{candidate}".')

        mime_type, _ = mimetypes.guess_type(candidate.name)
        if mime_type is None or not mime_type.startswith("image/"):
            raise ValueError(f'Image item #{index} is not a supported image file: "{candidate}".')

        normalized.append(
            {
                "image_path": str(candidate),
                "display_path": _make_relative_to_run_dir(candidate, run_dir),
                "image_info": image_info.strip(),
            }
        )

    return normalized
