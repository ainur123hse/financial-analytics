from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_LISTED_IMAGES_PER_DIR = 2


def _render_directory_tree(directory_path: Path, depth: int) -> list[str]:
    indent = "  " * depth
    entries = sorted(directory_path.iterdir(), key=lambda item: item.name.lower())
    directories = [entry for entry in entries if entry.is_dir()]
    files = [entry for entry in entries if entry.is_file()]
    non_image_files = [file_path for file_path in files if file_path.suffix.lower() not in IMAGE_EXTENSIONS]
    image_files = [file_path for file_path in files if file_path.suffix.lower() in IMAGE_EXTENSIONS]

    lines: list[str] = []
    for sub_dir in directories:
        lines.append(f"{indent}{sub_dir.name}/")
        lines.extend(_render_directory_tree(sub_dir, depth=depth + 1))

    for file_path in non_image_files:
        lines.append(f"{indent}{file_path.name}")

    for file_path in image_files[:MAX_LISTED_IMAGES_PER_DIR]:
        lines.append(f"{indent}{file_path.name}")

    hidden_images_count = len(image_files) - MAX_LISTED_IMAGES_PER_DIR
    if hidden_images_count > 0:
        lines.append(f"{indent}... (+{hidden_images_count} image files)")

    return lines


def make_working_dir_desk(md_dir_path: Path) -> str:
    source_dir = Path(md_dir_path).expanduser().resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Markdown directory not found: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"Path is not a directory: {source_dir}")

    desk_lines = ["./"]
    desk_lines.extend(_render_directory_tree(source_dir, depth=1))
    if len(desk_lines) == 1:
        desk_lines.append("  <empty>")
    return "\n".join(desk_lines)


def make_system_prompt(md_dir_path: Path):

    working_dir_desk = make_working_dir_desk(md_dir_path)

    output_format = """{
  "code_to_execute": string | null,
  "dependencies": list[string] | null,
  "image_questions": [{
    "image_path": string,
    "question": string
  }, ...] | null,
  "final_answer": string | null
}"""

    system_prompt = f"""Ты аналитический агент по финансовым документам.
Тебе нужно ответить на вопрос пользователя, опираясь на данные из markdown-документов.

Структура рабочей директории:
{working_dir_desk}

В маркдаун документах есть ссылки на изображения (например: графиков, диаграмм и т.д)
Ccылки оформлены в формате: "![Image](image_path) Краткое описание: ```Краткое описание изображения```"
Python-код выполняется из корня рабочей директории.

Правила анализа:
1. Если можешь выдать итоговый ответ, верни его в "final_answer", а "code_to_execute", "dependencies", "image_questions" поставь в null.
2. Если нужен Python-расчёт/парсинг, заполни "code_to_execute".
3. Если для ответа нужно уточнение по изображению, ссылка на которую есть в маркдауне, заполни "image_questions".
4. Поля "code_to_execute" и "image_questions" можно использовать одновременно в одной итерации, если это действительно нужно.
5. Если "code_to_execute" не null:
   - код должен быть исполнимым как `python -c "..."`
   - выводи ключевые промежуточные результаты через print
   - если внешние библиотеки не нужны, ставь "dependencies": []
   - если внешние библиотеки нужны, укажи их в "dependencies" (например, ["pandas"])
6. Если "code_to_execute" = null, то "dependencies" должно быть null.
7. Не выдумывай данные. Если данных недостаточно, сначала запроси следующий инструментальный шаг через JSON.

ВАЖНО: возвращай ТОЛЬКО валидный JSON-объект (без markdown, без ```json, без комментариев и текста вокруг).

Всегда используй ровно такую схему и всегда заполняй все поля:
{output_format}"""
    return system_prompt
