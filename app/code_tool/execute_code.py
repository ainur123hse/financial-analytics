from pathlib import Path
import subprocess
import traceback
import venv
from app.schema import CodeExecutionResult


def _get_venv_python_executable(virtual_env_path: Path) -> Path:
    if Path(virtual_env_path, "Scripts", "python.exe").is_file():
        return Path(virtual_env_path, "Scripts", "python.exe")
    return Path(virtual_env_path, "bin", "python")


def _ensure_virtual_environment(virtual_env_path: Path) -> None:
    python_executable = _get_venv_python_executable(virtual_env_path)
    if python_executable.is_file():
        return

    virtual_env_path.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(str(virtual_env_path))


def install_dependencies_if_not_already_exist(virtual_env_path: Path, dependencies: list[str]) -> None:
    venv_path = Path(virtual_env_path).expanduser().resolve()
    _ensure_virtual_environment(venv_path)

    if not dependencies:
        return

    python_executable = _get_venv_python_executable(venv_path)
    try:
        # pip will skip already satisfied packages in this environment.
        subprocess.run(
            [str(python_executable), "-m", "pip", "install", *dependencies],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"Dependency installation failed: {details}") from exc


def execute_python_code_and_parse_result(
    python_code: str,
    dependencies: list[str],
    working_dir_path: Path,
    virtual_env_path: Path,
) -> CodeExecutionResult:
    try:
        install_dependencies_if_not_already_exist(
            virtual_env_path=virtual_env_path,
            dependencies=dependencies,
        )
    except Exception:
        return CodeExecutionResult(
            success=False,
            exception_with_traceback=traceback.format_exc(),
            stdout="",
        )

    working_dir = Path(working_dir_path).expanduser().resolve()
    working_dir.mkdir(parents=True, exist_ok=True)
    python_executable = _get_venv_python_executable(Path(virtual_env_path).expanduser().resolve())

    completed = subprocess.run(
        [str(python_executable), "-c", python_code],
        cwd=str(working_dir),
        text=True,
        capture_output=True,
    )

    if completed.returncode == 0:
        return CodeExecutionResult(
            success=True,
            exception_with_traceback=None,
            stdout=completed.stdout,
        )

    exception = completed.stderr.strip()
    if not exception:
        exception = f"Python process exited with code {completed.returncode}."

    return CodeExecutionResult(
        success=False,
        exception_with_traceback=exception,
        stdout=completed.stdout,
    )
