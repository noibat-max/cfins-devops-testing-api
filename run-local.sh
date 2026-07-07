#!/usr/bin/env bash
# Run the API locally against real AWS (cfins-local profile).
# Loads .env if present, then starts uvicorn with hot-reload on :8000.
set -euo pipefail

cd "$(dirname "$0")"

# Load .env (KEY=VALUE lines) into the environment if it exists.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "warning: no .env found — copy .env.example to .env first" >&2
fi

# Sensible fallbacks so the server still boots without a full .env.
export AWS_PROFILE="${AWS_PROFILE:-cfins-local}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

# Prefer the shared venv one level up (created for the scripts/), else system python.
PY="../.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

echo "Starting QA Workbench API — profile=$AWS_PROFILE region=$AWS_REGION"
exec "$PY" -m uvicorn app.main:app --reload --port 8000
