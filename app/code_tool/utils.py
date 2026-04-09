import shutil
from pathlib import Path


def prepare_code_execution_working_dir(
    md_path: Path,
    md_images_dir_path: Path,
    code_execution_working_dir: Path
) -> str:
    source_md_path = Path(md_path).expanduser().resolve()
    source_images_dir_path = Path(md_images_dir_path).expanduser().resolve()

    if not source_md_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {source_md_path}")
    if not source_images_dir_path.is_dir():
        raise FileNotFoundError(f"Images directory not found: {source_images_dir_path}")

    if code_execution_working_dir.exists():
        if code_execution_working_dir.is_dir():
            shutil.rmtree(code_execution_working_dir)
        else:
            code_execution_working_dir.unlink()
    code_execution_working_dir.mkdir(parents=True, exist_ok=True)

    relative_md_path = source_md_path.name
    shutil.copy(source_md_path, code_execution_working_dir / relative_md_path)
    for image_path in source_images_dir_path.iterdir():
        if image_path.is_file():
            shutil.copy(image_path, code_execution_working_dir / image_path.name)

    return relative_md_path