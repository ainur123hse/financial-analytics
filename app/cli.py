import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

from app.agent import DEFAULT_MODEL, DEFAULT_MAX_ITERATIONS, run_agent
from app.observability import shutdown_observability


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Financial analyst agent for PDF documents.")
    parser.add_argument("--pdf", required=True, help="Path to the PDF document.")
    parser.add_argument("--prompt", required=True, help="User question for the analyst.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model name.")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="Maximum number of code iterations before forced summarization.",
    )
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        final_answer = asyncio.run(
            run_agent(
                user_prompt=args.prompt,
                path_to_pdf=Path(args.pdf),
                model=args.model,
                max_iterations=args.max_iterations,
            )
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        shutdown_observability()

    print(final_answer)
    return 0

if __name__ == "__main__":
    raise SystemExit(cli())
