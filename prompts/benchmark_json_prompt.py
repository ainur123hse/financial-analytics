from pathlib import Path
from textwrap import dedent

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_LISTED_IMAGES_PER_DIR = 2
ANALYTICS_DIRNAME = "аналитика"
SOURCES_DIRNAME = "источники"


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


def _make_directory_tree(dataset_root: Path) -> str:
    desk_lines = ["./"]
    desk_lines.extend(_render_directory_tree(dataset_root, depth=1))
    if len(desk_lines) == 1:
        desk_lines.append("  <empty>")
    return "\n".join(desk_lines)


def _resolve_dataset_root(dataset_root: str | Path) -> tuple[Path, str]:
    raw_path = Path(dataset_root).expanduser()
    resolved_path = raw_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Dataset root not found: {resolved_path}")
    if not resolved_path.is_dir():
        raise ValueError(f"Path is not a directory: {resolved_path}")
    return resolved_path, str(raw_path)


def make_prompt(dataset_root: str | Path) -> str:
    dataset_dir, dataset_root_display = _resolve_dataset_root(dataset_root)

    analytics_dir = dataset_dir / ANALYTICS_DIRNAME
    sources_dir = dataset_dir / SOURCES_DIRNAME
    if not analytics_dir.is_dir():
        raise FileNotFoundError(f"Analytics directory not found: {analytics_dir}")
    if not sources_dir.is_dir():
        raise FileNotFoundError(f"Sources directory not found: {sources_dir}")

    directory_tree = _make_directory_tree(dataset_dir)
    output_schema = """{
  "2": {
    "analytics": ["аналитика/<файл_периода_1>.md", "..."],
    "sources": ["источники/<релевантный_источник>.md", "..."],
    "generated_analysis_period_description": "например: июль 2025 или I квартал 2026 года, результаты 1 квартала 2026 года",
    "generated_analysis_name": "<имя_эталонного_файла_периода_N>-generated.md",
    "reference_analysis_path": "аналитика/<эталонный_файл_периода_N>.md"
  },
  "3": {
    "analytics": ["аналитика/<файл_периода_1>.md", "аналитика/<файл_периода_2>.md"],
    "sources": ["источники/<релевантный_источник>.md", "..."],
    "generated_analysis_period_description": "...",
    "generated_analysis_name": "...-generated.md",
    "reference_analysis_path": "аналитика/<эталонный_файл_периода_3>.md"
  }
}"""

    return dedent(
        f"""
        Ты готовишь benchmark-конфигурацию для восстановления аналитики по компании.
        Рабочий корень датасета: `{dataset_root_display}`.

        Внутри него ожидаются:
        - `{ANALYTICS_DIRNAME}/` — человеческие аналитические документы;
        - `{SOURCES_DIRNAME}/` — первичные документы-источники, на которые аналитика могла опираться.

        Структура датасета:
        {directory_tree}

        Твоя задача:
        1. Исследовать все файлы в `{ANALYTICS_DIRNAME}/` и восстановить реальную хронологию периодов.
        2. Использовать имена файлов как первую подсказку, но при любой неоднозначности обязательно открывать сами markdown-файлы, читать заголовки и содержимое.
        3. Для каждого периода `N >= 2` собрать JSON-объект для benchmark.

        Что считать периодом:
        - Период — это отдельный шаг хронологии аналитики с конкретным инвестиционным взглядом автора.

        Как заполнять поля для каждого периода `N`:
        - `analytics`: все аналитические документы из предыдущих периодов `1 .. N-1`. Используй относительные пути от корня датасета. Список отсортируй по хронологии.
        - `sources`: набор всех источников, информация из которых не позже периодов `1 .. N`, включай сюда все подходящие источники, даже если они нерелевантны для аналитики. Используй относительные пути от корня датасета.
        - `generated_analysis_period_description`: краткое человеческое описание целевого периода для генерации новой аналитики. Пиши по-русски, например: `июль 2025` или `I квартал 2026 года, результаты 1 квартала 2026 года`.
        - `generated_analysis_name`: только имя файла, без пути. Возьми `reference_analysis_path`, оставь только basename и замени суффикс `.md` на `-generated.md`.
        - `reference_analysis_path`: аналитический файл именно целевого периода `N`. Используй относительный путь.

        Правила отбора источников и защиты от утечки будущих данных:
        - Для периода `N` можно включать только те источники, которые были доступны автору не позже этого периода.
        - Не включай документы, опубликованные позже целевого периода `N`, или документы, которые явно описывают факты и результаты, ставшие известными только после периода `N`.
        - Если документ был доступен в период `N`, но внутри него есть прогнозы, guidance или ожидания на более поздние даты, это не считается утечкой: такой документ можно включать.

        Ограничения:
        - Верхнеуровневые ключи JSON должны быть строками `"2"`, `"3"`, ..., по возрастанию без пропусков.
        - Не создавай ключ `"1"`.
        - Не выдумывай файлы, которых нет в дереве датасета.
        - Все пути в JSON должны быть относительными к `{dataset_root_display}`.
        - Возвращай только валидный JSON-объект без markdown fences, без комментариев и без поясняющего текста вокруг.

        Используй ровно такую схему JSON:
        {output_schema}
        
        Создай json: {dataset_root_display}/bench_info.json
        
        После этого дополнительно побей файл с котировками и другими табличными данными если он есть и если требуется его разбивать согласно хронологии и тоже добавь пути в источники в json
        """
    ).strip()

p = make_prompt("/home/ainur/dissertation/financial-analytics/dataset/лента")
print(p)