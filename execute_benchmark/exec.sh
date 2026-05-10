#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
env_file="${repo_root}/.env"

if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
fi

if [[ -z "${OPENROUTER_API_KEY:-}" && -n "${LLM_API_KEY:-}" ]]; then
    export OPENROUTER_API_KEY="${LLM_API_KEY}"
fi

cd "${repo_root}"
exec python -m execute_benchmark.run_benchmark "$@"
