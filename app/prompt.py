import textwrap
from pathlib import Path
from typing import Any


def _build_initial_prompts(user_prompt: str, run_dir: Path) -> list[tuple[str, str | None]]:
    prompt = textwrap.dedent(f"{user_prompt}").strip()
    return [(prompt, None)]


def _build_execution_result_prompts(result: dict[str, Any]) -> list[tuple[str, str | None]]:
    lines = [
        f"Результат выполнения шага {result['iteration']}: {result['status']}.",
        f"Скрипт: {result['script_path']}.",
        _format_dependency_install_status(result),
    ]
    if result["requested_dependencies"]:
        lines.append(f"Зависимости шага: {', '.join(result['requested_dependencies'])}.")

    if result["success"]:
        other_info = result["other_info"] or "Промежуточные текстовые наблюдения не были переданы."
        lines.append(f"other_info:\n{other_info}")
    else:
        lines.append(f"Ошибка выполнения: {result['error']}")
        if result["dependency_install_status"] == "error":
            if result["dependency_install_stdout"].strip():
                lines.append(
                    "stdout установки зависимостей:\n"
                    f"{result['dependency_install_stdout'].strip()}"
                )
            if result["dependency_install_stderr"].strip():
                lines.append(
                    "stderr установки зависимостей:\n"
                    f"{result['dependency_install_stderr'].strip()}"
                )
            lines.append(
                "Исправь список зависимостей, код или, если данных уже достаточно, "
                "верни финальный ответ."
            )
        else:
            if result["stdout"].strip():
                lines.append(f"stdout:\n{result['stdout'].strip()}")
            if result["stderr"].strip():
                lines.append(f"stderr:\n{result['stderr'].strip()}")
            lines.append("Исправь код или, если данных уже достаточно, верни финальный ответ.")

    prompts: list[tuple[str, str | None]] = []

    for image in result["images"]:
        prompts.append(
            (
                f"Артефакт {image['display_path']}: {image['image_info']}",
                image["image_path"],
            )
        )

    prompts.append(("\n\n".join(lines), None))
    return prompts


def _build_forced_summary_prompt(max_iterations: int) -> str:
    return textwrap.dedent(
        f"""
        Лимит кодовых итераций исчерпан: {max_iterations}.
        Не пиши новый код.
        На основе всего накопленного контекста верни только итог:

        <final_answer>
        ...markdown answer...
        </final_answer>

        Никакого текста вне тега. Существенные выводы сопроводи ссылками на страницы [p.N].
        """
    ).strip()


def _format_dependency_install_status(result: dict[str, Any]) -> str:
    status = result["dependency_install_status"]
    if status == "not_requested":
        return "Установка зависимостей: не требовалась."
    if status == "success":
        return "Установка зависимостей: выполнена успешно."
    return "Установка зависимостей: завершилась ошибкой."
