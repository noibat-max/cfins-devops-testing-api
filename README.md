# qa-platform-api

The Python **REST API (FastAPI on uvicorn)** for C&F's QA Workbench — the
lift-and-shift target for `sample-qa-studio/web-app/lambdas` (API Gateway +
Lambda replaced by a custom REST API). Datastore stays **DynamoDB**
(single table `cfins-qaworkbench`).

## Layout

```
app/
  main.py       FastAPI app: CORS, audit + logging middleware, config validation, GET /health
  config.py     env-driven settings (table, region, S3, ECS, JWT, CORS, Cognito)
  aws.py        boto3 helpers (default credential chain — no hardcoded profile)
  security.py   issuer-routed auth (local JWT / Cognito / PAT) + require_scopes
  audit.py      records every mutating request (payload redacted) to DynamoDB
  routers/
    shell/      app-agnostic platform: auth, apps, users, groups, tokens, audit  (mounted /api)
    nova/       Nova Act QA Studio: usecases, steps, config, executions, templates, suites  (mounted /api/qawb)
scripts/        provisioning + seed tooling (local-only, excluded from image)
docs/           api-surface.md, remote-execution-ecs.md, api-task-role-policy.json
run-local.sh    load .env → uvicorn --reload on :8000
.env.example    documents every env var (copy to .env)
DEPLOYMENT.md   DevOps hand-over: image + ECS runtime contract
```

## Run locally

Requires the shared venv one level up (`../.venv`) with deps installed:

```bash
../.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then set JWT_SIGN_HASH
./run-local.sh                  # -> http://localhost:8000
```

Verify:

```bash
curl -s localhost:8000/health   # {"status":"ok"}
open http://localhost:8000/docs # Swagger UI
```

Runs against **real AWS** via the `cfins-local` profile (boto3 default
credential chain). On ECS the task role is used instead — same code, no profile.

## Docker & ECR

Build once, region/env-agnostic — all config comes from the environment (the ECS
task definition); **no secrets or AWS credentials are baked in** (the task role +
Secrets Manager supply them at runtime). `scripts/` and `.env` are excluded via
`.dockerignore`. See `Dockerfile`.

Build the image — must be **`linux/amd64`** (Fargate x86):

```bash
docker build -t cfins-qaworkbench-api:dev .
# Apple Silicon: docker buildx build --platform linux/amd64 -t cfins-qaworkbench-api:dev --load .
```

Smoke-test locally (mount the profile to stand in for the task role):

```bash
docker run --rm -p 8001:8000 \
  -e JWT_SIGN_HASH=smoke -e AWS_PROFILE=cfins-local -e AWS_REGION=us-east-1 \
  -v ~/.aws:/home/app/.aws:ro cfins-qaworkbench-api:dev
curl -s localhost:8001/health   # {"status":"ok"}
```

Push to ECR (account `103930328611`, `us-east-1`):

```bash
REG=103930328611.dkr.ecr.us-east-1.amazonaws.com

# 1. Create the repo — ONE TIME (skip on later pushes)
aws ecr create-repository --repository-name cfins-qaworkbench-api \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
  --region us-east-1 --profile cfins-local

# 2. Log Docker in to ECR (token ~12h)
aws ecr get-login-password --region us-east-1 --profile cfins-local \
  | docker login --username AWS --password-stdin $REG

# 3. Tag with an immutable version (semver or git SHA), then push
docker tag  cfins-qaworkbench-api:dev $REG/cfins-qaworkbench-api:0.1.0
docker push $REG/cfins-qaworkbench-api:0.1.0
```

Notes:
- **Immutable tags** → each push needs a **new** tag; the repo already exists after step 1.
- **IAM:** the dev `cfins-local-ecs-provisioning` grant scopes ECR to the *runner*
  repo — to create/push `cfins-qaworkbench-api`, add that repo's ARN (or widen to
  `repository/cfins-qaworkbench-*`).
- **Runtime shape:** the API is a long-lived ECS **Service** behind an ALB (health
  check on `/health`), unlike the one-shot runner task. Its **task role** needs
  `cfins-qaworkbench*` DynamoDB/S3/Secrets **plus** `ecs:RunTask` + `iam:PassRole`
  (for the `run_now` remote-execution trigger).
- CI/CD build+push, multi-env task defs, and pinned digests are **DevOps** (out of scope).

## Deploying to ECS

DevOps hand-over — the image + runtime contract (env vars, secrets, IAM, health,
the GitLab→ECR→ECS pipeline) is in **[`DEPLOYMENT.md`](DEPLOYMENT.md)**. The
`run_now` remote-execution trigger is in **`docs/remote-execution-ecs.md`**; the
task-role policy is **`docs/api-task-role-policy.json`**.

## Status

Built + verified against real AWS: local + Cognito auth, PATs, scoped authZ,
Use Cases + Steps, Templates (+ sync), Test Suites (authoring + execution),
the §5 execution engine, remote **Run Now** → ECS Fargate (use case + suite),
audit trail, and Users/Groups admin. The full API surface is in
`docs/api-surface.md`. Remaining work is DevOps productionization (CI/CD,
multi-env task defs, pinned digests) and the deferred `queued`/`scheduled`
execution modes.
