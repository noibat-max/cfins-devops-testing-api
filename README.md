# cfins-devops-testing-api

The Python **REST API (FastAPI on uvicorn)** for C&F's QA Workbench — the
lift-and-shift target for `sample-qa-studio/web-app/lambdas` (API Gateway +
Lambda replaced by a custom REST API). Datastore stays **DynamoDB**
(single table `cfins-qaworkbench`).

## Layout

```
app/
  main.py       FastAPI app: CORS, config validation, GET /health
  config.py     env-driven settings (table, region, JWT, CORS)
  aws.py        boto3 helpers (default credential chain — no hardcoded profile)
  routers/      API routers (empty until auth phase)
scripts/        provisioning + seed tooling (local-only, excluded from image)
run-local.sh    load .env → uvicorn --reload on :8000
.env.example    documents every env var (copy to .env)
```

## Run locally

Requires the shared venv one level up (`../.venv`) with deps installed:

```bash
../.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then set JWT_SECRET
./run-local.sh                  # -> http://localhost:8000
```

Verify:

```bash
curl -s localhost:8000/health   # {"status":"ok"}
open http://localhost:8000/docs # Swagger UI
```

Runs against **real AWS** via the `cfins-local` profile (boto3 default
credential chain). On ECS the task role is used instead — same code, no profile.

## Status

Phase 1 (scaffold) done: app boots, `/health` responds, config loads. Auth
endpoints (`/auth/login`, `/auth/me`) and the scope middleware come next.
