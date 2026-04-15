import shutil
from pathlib import Path


def prepare_code_execution_working_dir(
    md_dir_path: Path,
    code_execution_working_dir: Path
) -> None:
    source_dir = Path(md_dir_path).expanduser().resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Markdown directory not found: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"Path is not a directory: {source_dir}")

    if code_execution_working_dir.exists():
        if code_execution_working_dir.is_dir():
            shutil.rmtree(code_execution_working_dir)
        else:
            code_execution_working_dir.unlink()
    code_execution_working_dir.mkdir(parents=True, exist_ok=True)

    for item in source_dir.iterdir():
        destination = code_execution_working_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
