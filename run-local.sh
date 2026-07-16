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

# Locate a venv Python across layouts: in-repo (.venv) or shared one level up
# (../.venv), Unix (bin/python) or Windows/Git-Bash (Scripts/python.exe).
PY=""
for cand in \
  .venv/bin/python .venv/Scripts/python.exe \
  ../.venv/bin/python ../.venv/Scripts/python.exe; do
  if [[ -x "$cand" ]]; then PY="$cand"; break; fi
done
# Fall back to whatever Python is on PATH (Windows uses `python`, not `python3`).
[[ -n "$PY" ]] || PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
  echo "error: no venv or system Python found. Create one: python -m venv .venv && pip install -r requirements.txt" >&2
  exit 1
fi

echo "Starting QA Workbench API — python=$PY profile=$AWS_PROFILE region=$AWS_REGION"
exec "$PY" -m uvicorn app.main:app --reload --port 8000
