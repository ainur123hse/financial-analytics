from pathlib import Path
from typing import Any, Sequence

from app.answer_parsing import parse_llm_answer
from app.code_execution import execute_code, _prepare_run_dir
from app.llm_client import LLMClient
from app.observability import (
    build_traced_user_content,
    propagate_attributes,
    sanitize_chat_messages,
    serialize_for_langfuse,
    start_observation,
    update_observation,
)
from app.prompt import _build_initial_prompts, _build_execution_result_prompts, _build_forced_summary_prompt
from app.system_prompt import system_prompt
from app.utils import _validate_pdf_path, _validation_error

DEFAULT_MODEL = "openai/gpt-5.4"
DEFAULT_MAX_ITERATIONS = 6

class AgentRuntimeError(RuntimeError):
    """Raised when the agent cannot produce a valid answer."""

def to_stop(llm_answer: dict[str, Any]) -> bool:
    return llm_answer.get("kind") == "final_answer"

def _build_history_content(prompt: str, image_path: str | None) -> str | list[dict[str, Any]]:
    if image_path is None:
        return prompt

    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": LLMClient._image_path_to_data_url(image_path)},
        },
    ]

def add_to_history(
    history: list[dict[str, Any]],
    prompts_with_images: Sequence[tuple[str, str | None]],
    assistant_answer: str,
) -> None:
    for prompt, image_path in prompts_with_images:
        history.append(
            {
                "role": "user",
                "content": _build_history_content(prompt, image_path),
            }
        )
    history.append({"role": "assistant", "content": assistant_answer})


async def ask(
    client: LLMClient,
    history: list[dict[str, Any]],
    prompts_with_images: list[tuple[str, str | None]],
    model: str,
    *,
    phase: str,
    iteration: int | None,
    final_only: bool = False,
) -> dict[str, Any]:
    current_prompts = prompts_with_images

    observation_input = {
        "history": sanitize_chat_messages(history),
        "prompts": [
            {
                "role": "user",
                "content": build_traced_user_content(
                    prompt=prompt,
                    image_paths=[image_path] if image_path is not None else None,
                ),
            }
            for prompt, image_path in current_prompts
        ],
        "final_only": final_only,
    }
    observation_metadata: dict[str, Any] = {
        "phase": phase,
        "final_only": final_only,
        "prompt_count": len(current_prompts),
        "history_length": len(history),
    }
    if iteration is not None:
        observation_metadata["iteration"] = iteration

    with start_observation(
        name="agent.ask",
        as_type="chain",
        input=serialize_for_langfuse(observation_input),
        metadata=serialize_for_langfuse(observation_metadata),
    ) as observation:
        try:
            llm_answer = await client.ask(
                history=history,
                system_prompt=system_prompt,
                prompts_with_images=current_prompts,
                phase=phase,
                iteration=iteration,
                reasoning=False,
                model=model,
            )
            parsed_answer = parse_llm_answer(llm_answer)
            validation_error = _validation_error(parsed_answer, final_only)
            add_to_history(history, current_prompts, llm_answer)

            update_observation(
                observation,
                output=serialize_for_langfuse(
                    {
                        "llm_answer": llm_answer,
                        "parsed_answer": parsed_answer,
                        "parsed_kind": parsed_answer["kind"],
                        "validation_error": validation_error,
                    }
                ),
            )

            if validation_error is None:
                return parsed_answer

            update_observation(
                observation,
                level="ERROR",
                status_message=validation_error,
            )
            raise Exception(validation_error)
        except Exception as exc:
            update_observation(
                observation,
                level="ERROR",
                status_message=str(exc),
            )
            raise

async def run_agent(
    user_prompt: str,
    path_to_pdf: Path,
    model: str = DEFAULT_MODEL,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> str:
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1.")

    pdf_path = _validate_pdf_path(path_to_pdf)
    run_dir = _prepare_run_dir(pdf_path)
    history: list[dict[str, Any]] = []
    prompts_with_images = _build_initial_prompts(user_prompt, run_dir)

    with start_observation(
        name="agent.run",
        as_type="agent",
        input=serialize_for_langfuse(
            {
                "user_prompt": user_prompt,
                "pdf_filename": pdf_path.name,
                "model": model,
                "max_iterations": max_iterations,
            }
        ),
        metadata=serialize_for_langfuse(
            {
                "pdf_path": str(pdf_path),
                "run_dir": str(run_dir),
            }
        ),
    ) as observation:
        with propagate_attributes(
            session_id=run_dir.name,
            trace_name="agent.run",
            metadata={"model": model, "pdf_filename": pdf_path.name},
            tags=["cli", "pdf-agent"],
        ):
            try:
                async with LLMClient() as client:
                    for iteration in range(1, max_iterations + 1):
                        llm_answer_parsed = await ask(
                            client=client,
                            history=history,
                            prompts_with_images=prompts_with_images,
                            model=model,
                            phase="initial" if iteration == 1 else "iteration",
                            iteration=iteration,
                        )
                        if to_stop(llm_answer_parsed):
                            final_answer = llm_answer_parsed["final_answer"]
                            update_observation(observation, output=final_answer)
                            return final_answer

                        code_res = execute_code(
                            llm_answer_parsed["code"],
                            dependencies=llm_answer_parsed["dependencies"],
                            run_dir=run_dir,
                            iteration=iteration,
                        )
                        prompts_with_images = _build_execution_result_prompts(code_res)

                    forced_summary_prompts = list(prompts_with_images)
                    forced_summary_prompts.append((_build_forced_summary_prompt(max_iterations), None))
                    forced_summary = await ask(
                        client=client,
                        history=history,
                        prompts_with_images=forced_summary_prompts,
                        model=model,
                        phase="forced_summary",
                        iteration=max_iterations + 1,
                        final_only=True,
                    )
                    if not to_stop(forced_summary):
                        raise AgentRuntimeError("Forced summary did not produce a final answer.")

                    final_answer = forced_summary["final_answer"]
                    update_observation(observation, output=final_answer)
                    return final_answer
            except Exception as exc:
                update_observation(
                    observation,
                    level="ERROR",
                    status_message=str(exc),
                    output={"error": str(exc)},
                )
                raise
