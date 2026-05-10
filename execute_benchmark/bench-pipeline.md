# Execute Benchmark Pipeline

`execute_benchmark/exec.sh` is the user-facing entrypoint. It delegates to
`python -m execute_benchmark.run_benchmark`.

## What It Does

The new runner is tmux-based and interactive:

1. Selects benchmark periods from `bench_info.json`.
2. Refuses to start more than 4 periods in one run.
3. Creates isolated staging folders under `/tmp/analyze/<run_id>/<period>/generation/`
   for every selected period before opening tmux.
4. Copies only benchmark-visible inputs into each generation folder:
   - `аналитика/` with previous human analytics;
   - `документы/` with allowed source documents.
5. Writes prompt files into `benchmark_results/<run_id>/<period>/`:
   - `generation.prompt.txt`
   - `evaluation.prompt.txt`
6. Creates one tmux window and splits it into 1-4 panes.
7. In each pane, starts a Python worker for exactly one period.
8. Each worker launches plain `codex` CLI for generation inside that period's
   staged `generation/` directory.
9. After the generated file is ready and the Codex session is closed, the worker:
   - copies the generated markdown to `benchmark_results/<run_id>/<period>/generated/`;
   - creates a separate `evaluation/` folder with `candidate/`, `reference/`,
     and `evaluation.schema.json`;
   - launches plain `codex` CLI again with the evaluation prompt.
10. After the evaluation session is closed, the worker validates
    `evaluation.raw.json`, writes:
    - `benchmark_results/<run_id>/<period>/evaluation.json`
    - `benchmark_results/<run_id>/<period>/result.json`
11. When all period workers finish, the run-level summary is assembled:
    - `benchmark_results/<run_id>/summary.json`
    - `benchmark_results/<run_id>/summary.md`

Generation and evaluation logs are stored next to period results. They contain
launch metadata and exit codes only; tmux terminal transcripts are not captured.

## Interactive Behavior

- The workflow uses `codex`, not `codex exec`.
- Each pane runs generation first, then evaluation for the same period.
- The worker continues to evaluation after the generation Codex session exits.
- If the expected output file was not created, that period is marked as failed.

## Current Evaluation Contract

The implemented runner evaluates against `reference_analysis_path`, not against
`main_chronology.md`.

Each `evaluation.json` contains:

- `period`
- `generated_analysis_name`
- `reference_analysis_paths`
- `reference_claims_count`
- `generated_claims_count`
- `matched_claims_count`
- `precision`
- `recall`
- `f1`
- `missing_reference_claims`
- `unsupported_generated_claims`
- `comment`

## Usage

Run all available periods, attach to tmux immediately:

```bash
./execute_benchmark/exec.sh dataset/северсталь
```

Run specific periods:

```bash
./execute_benchmark/exec.sh dataset/северсталь --period 11 --period 12
```

Create the tmux session without attaching:

```bash
./execute_benchmark/exec.sh dataset/северсталь --detach
```

Use a stub or alternate Codex binary:

```bash
./execute_benchmark/exec.sh dataset/северсталь --codex-bin /path/to/stub
```
