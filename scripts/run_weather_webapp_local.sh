#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

# Optional first argument: explicit AZD .env path.
# Example: ./scripts/run_weather_webapp_local.sh .azure/bubble-chat/.env
source "$script_dir/load_azd_env.sh" "${1:-}"

cd "$repo_root/function"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed or not on PATH." >&2
  exit 1
fi

if ! command -v func >/dev/null 2>&1; then
  echo "Error: Azure Functions Core Tools (func) is not installed or not on PATH." >&2
  echo "This devcontainer should provide func automatically. Rebuild the container if needed." >&2
  exit 1
fi

ensure_python_tooling() {
  local -a packages=()

  if ! python3 -m pip --version >/dev/null 2>&1; then
    packages+=(python3-pip)
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    packages+=(python3-venv)
  fi

  if [[ "${#packages[@]}" -eq 0 ]]; then
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "Error: missing Python tooling (${packages[*]}) and sudo is unavailable." >&2
    exit 1
  fi

  echo "Installing missing Python tooling: ${packages[*]}"
  sudo apt-get update -y
  sudo apt-get install -y "${packages[@]}"
}

ensure_python_tooling

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating weather webapp virtual environment..."
  python3 -m venv .venv
fi

PYTHON_BIN="$(pwd)/.venv/bin/python"

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  "$PYTHON_BIN" -m ensurepip --upgrade || true
fi

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  echo "Error: pip is unavailable in weather webapp virtual environment." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c "import azure.functions" >/dev/null 2>&1; then
  echo "Installing weather function dependencies..."
  "$PYTHON_BIN" -m pip install -r requirements.txt
fi

export PATH="$(pwd)/.venv/bin:$PATH"

# Ensure local runtime defaults when not present in local settings.
export FUNCTIONS_WORKER_RUNTIME="${FUNCTIONS_WORKER_RUNTIME:-python}"
export AzureWebJobsScriptRoot="$(pwd)"

echo "Starting Weather Function App on http://localhost:${WEATHER_WEBAPP_PORT:-7071}"

exec func start \
  --port "${WEATHER_WEBAPP_PORT:-7071}" \
  --python
