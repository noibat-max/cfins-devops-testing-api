# Deploying `qa-platform-api` — DevOps hand-over

This is the **single source of truth** for deploying the QA Workbench REST API to
AWS ECS. It defines the **image contract** the CI pipeline must produce and the
**runtime contract** (env vars, secrets, IAM, networking, health) the ECS task
definition must satisfy.

- **App** — FastAPI on uvicorn, one long-lived process, port **8000**.
- **Datastore** — DynamoDB single table (`cfins-qaworkbench`) + S3 artifacts + Secrets Manager.
- **Runtime shape** — a long-lived ECS **Service** behind an **ALB** (health check on `/health`).
  (This is unlike the *runner*, which is a one-shot Fargate task — see the CLI repo doc.)

Verified end-to-end in the dev account (`103930328611`, `us-east-1`).

Related docs:
- **`docs/api-task-role-policy.json`** — the API task-role IAM policy (attach as-is).
- **`docs/remote-execution-ecs.md`** — the `run_now` remote-execution trigger (API side).
- **`cfins-devops-testing-cli/docs/runner-ecs.md`** — the runner image/task the API launches.

---

## 1. Pipeline flow (what DevOps owns)

```
git push ─► GitLab CI (.gitlab-ci.yml)
              1. docker build  (linux/amd64, immutable tag)
              2. push ─► GitLab Container Registry
              3. push/mirror ─► Amazon ECR (cfins-qaworkbench-api:<tag>)
                                   │
                        ECS Service (new task-def revision → rolling deploy)
                                   │
                            ALB ─► API tasks on :8000  (health /health)
```

DevOps owns the **`.gitlab-ci.yml`** and the ECR mirror + ECS deploy steps. This
repo provides the **`Dockerfile`** (the image contract) and everything below.

**The pipeline must:**
1. Build from the repo `Dockerfile` for **`linux/amd64`** (Fargate is x86_64).
2. Tag **immutably** — semver or the Git SHA, never a moving tag for SAT/prod.
3. Push to the GitLab Container Registry, then push the same image to **ECR**
   (`103930328611.dkr.ecr.us-east-1.amazonaws.com/cfins-qaworkbench-api:<tag>`).
   The ECR repo should be **tag-immutable**, scan-on-push.
4. Register a new **ECS task-definition revision** pointing at the pushed image
   **digest** and update the **Service**.

**Build once, promote the digest.** The image is region/env-agnostic — no config,
no secrets, no AWS keys baked in. DEV, SAT, prod all run the *same* image; only the
task-definition env/secrets/roles differ (§4–§6). Promote to prod by pointing the
prod task def at an already-vetted digest — never rebuild per environment.

---

## 2. Image contract (from `Dockerfile` — do not change without coordinating)

| Property | Value |
|---|---|
| Base | `python:3.11-slim` |
| Platform | **`linux/amd64`** (required for Fargate x86_64) |
| Port | **8000** (uvicorn, `--host 0.0.0.0`, no `--reload`) |
| User | **non-root** (`app`, uid `10001`) |
| Health | `HEALTHCHECK` hits `/health` via stdlib (for local `docker run`; ECS/ALB use their own) |
| Secrets baked in | **none** — creds via task role, `JWT_SIGN_HASH` via Secrets Manager |
| Excluded from image | `scripts/`, `docs/`, `.env*`, tests, caches (see `.dockerignore`) |
| Entrypoint | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |

One uvicorn worker per task; **scale horizontally via ECS task count**, not workers
(keeps memory + logging simple; the ALB spreads load).

---

## 3. IAM — two roles

### Task role (the API's runtime identity)
On ECS the app's boto3 code assumes the **task role** (resolved via the
container-credentials endpoint — no profile, no keys). Attach the policy in
**`docs/api-task-role-policy.json`** to a role (standard `ecs-tasks.amazonaws.com`
trust). It grants:

- `cfins-qaworkbench` DynamoDB (+ `index/*` for the `suite-execution-index` GSI)
- `cfins-qaworkbench-*` S3 (the presigned URLs the API mints are signed with these creds)
- `cfins-qaworkbench*` Secrets Manager (per-use-case secrets) + `ListSecrets`
- `ecs:RunTask` on `cfins-qaworkbench-runner:*` **+** `iam:PassRole` on
  `cfins-qaworkbench-runner-*` — the `run_now` trigger (§ remote-execution doc)

### Task-execution role (the ECS agent's identity)
Needs the managed **`AmazonECSTaskExecutionRolePolicy`** (ECR pull + CloudWatch
logs) — and **nothing else**. The JWT signing key is fetched by the **app itself
via the task role** at startup (§6), not injected by ECS, so no Secrets Manager
grant is needed on this role.

---

## 4. Runtime environment — the ECS task-definition contract

Every value the API reads is a plain env var (declared once in `app/config.py`;
nothing else reads the environment). All of these go in the task-def
`environment:` block — including `JWT_SIGN_HASH_SECRET`, which is only a **secret
reference (an ARN), not the secret itself** (§6). No task-def `secrets:` block is
required.

> **Do NOT set `AWS_PROFILE` on ECS.** It's local-dev only; on ECS the task role is
> resolved automatically. Setting it would break credential resolution.

### Required — every environment

| Var | Purpose | Kind | Example |
|---|---|---|---|
| `ENVIRONMENT` | Deploy env; stamped onto PATs so a token minted here is rejected elsewhere | config | `dev` / `sat` / `prod` |
| `AWS_REGION` | AWS region | config | `us-east-1` |
| `WORKBENCH_TABLE` | DynamoDB table | config | `cfins-qaworkbench` |
| `ARTIFACTS_BUCKET` | S3 bucket for artifacts (**per-env**) | config | `cfins-qaworkbench-dev` |
| `SECRET_PREFIX` | Secrets Manager name prefix for per-use-case secrets | config | `cfins-qaworkbench` |
| `CORS_ORIGINS` | Allowed UI origin(s), comma-separated (**must** be the real UI URL — default is localhost) | config | `https://qa.dev.cfins.internal` |
| `JWT_SIGN_HASH_SECRET` | Secrets Manager id/ARN of the HS256 signing key; the app fetches its value at startup via the task role (§6) | config (points at a secret) | `cfins-qaworkbench/dev/jwt-sign-hash` |

> **Local dev** supplies the key inline via `JWT_SIGN_HASH` instead (in `.env`); on
> ECS leave that blank and set `JWT_SIGN_HASH_SECRET`. One of the two is required.

### Optional — sensible defaults

| Var | Purpose | Default | Notes |
|---|---|---|---|
| `LOG_LEVEL` | `cfins.*` logger verbosity | `INFO` | `DEBUG` to trace an issue |
| `LOG_FORMAT` | Log format | `json` | **Keep `json` on ECS** — queryable in CloudWatch Logs Insights |
| `JWT_ISSUER` | `iss` claim on minted tokens | `cfins-qaworkbench` | rarely changed |
| `JWT_TTL_HOURS` | Token lifetime | `8` | |

### Remote execution — required only to enable **"Run Now"**
Leave **blank** to disable `run_now` (local CLI runs still work; the API returns a
clear **400** if Run Now is called). Values come from the runner infra
(`scripts/provision_ecs.py` output + the VPC).

| Var | Purpose | Default |
|---|---|---|
| `ECS_CLUSTER` | Cluster to launch runner tasks in | *(blank)* |
| `RUNNER_TASK_DEFINITION` | Runner task def — **bare family** (DEV, latest rev) or **`family:rev`** (SAT/prod, pinned) | *(blank)* |
| `RUNNER_SUBNETS` | Subnets for the runner task (CSV) | *(blank)* |
| `RUNNER_SECURITY_GROUPS` | Security groups (CSV) | *(blank)* |
| `RUNNER_LAUNCH_TYPE` | Launch type | `FARGATE` |
| `RUNNER_ASSIGN_PUBLIC_IP` | Public IP for egress | `ENABLED` |
| `RUNNER_CAPTURE` | Default artifact capture (`screenshots`\|`full`); per-run `capture` overrides | `screenshots` |

`run_now` is enabled only when `ECS_CLUSTER` + `RUNNER_TASK_DEFINITION` +
`RUNNER_SUBNETS` are all set. Full detail: **`docs/remote-execution-ecs.md`**.

### SSO (Cognito) — optional
Leave **blank** to disable SSO (local username/password login still works). These
are **public config, not secrets** — token validation uses the public JWKS
endpoint, so **no AWS credentials are needed for SSO**.

| Var | Purpose |
|---|---|
| `COGNITO_USER_POOL_ID` | e.g. `us-east-1_xxxxxxxxx` |
| `COGNITO_CLIENT_ID` | public SPA app-client id |
| `COGNITO_DOMAIN` | Managed-login base, e.g. `https://<prefix>.auth.us-east-1.amazoncognito.com` |

---

## 5. Per-environment values (what changes DEV → SAT → prod)

Same image everywhere; these task-def values differ per environment:

- `ENVIRONMENT`, `ARTIFACTS_BUCKET` (per-env bucket), `CORS_ORIGINS` (per-env UI URL)
- `JWT_SIGN_HASH` secret ARN (**a distinct secret per environment**)
- `COGNITO_*` (per-env user pool / client / domain)
- `RUNNER_TASK_DEFINITION` — **bare family in DEV**, **pinned `:rev` in SAT/prod**
- The **task role** and **execution role** ARNs (per-env, least-privilege)

`WORKBENCH_TABLE`, `SECRET_PREFIX`, `AWS_REGION`, `RUNNER_LAUNCH_TYPE` are typically
constant across envs (each env is its own account/table if you isolate that way).

---

## 6. `JWT_SIGN_HASH` from Secrets Manager (app-side fetch)

The API resolves its HS256 signing key at **startup**, using its **own task
role** — there is **no** task-def `secrets:` block and **no** execution-role
secret grant. Two inputs (`app/config.py`, resolved in `main._resolve_jwt_sign_hash`):

- **`JWT_SIGN_HASH`** — the key value directly. Used for **local dev** (`.env`).
  If set, it wins and AWS is never called.
- **`JWT_SIGN_HASH_SECRET`** — a Secrets Manager **secret id/ARN**. When
  `JWT_SIGN_HASH` is empty, the API calls `secretsmanager:GetSecretValue` on this
  id at boot and uses the returned `SecretString` as the key. If that string is
  JSON, set **`JWT_SIGN_HASH_SECRET_KEY`** to the field to extract.

On ECS you set **`JWT_SIGN_HASH_SECRET`** as a **plain env var** (it's an ARN, not
the secret) and leave `JWT_SIGN_HASH` unset.

1. Create one secret per environment holding a random 256-bit+ value — generate
   it with `scripts/gen_jwt_secret.py` and store **only the value** in Secrets
   Manager (never in git or the task def). **Name it under the workbench prefix**
   (e.g. `cfins-qaworkbench/dev/jwt-sign-hash`) so the API's task role — which
   already allows `GetSecretValue` on `cfins-qaworkbench*` (see
   `api-task-role-policy.json`) — can read it with **no policy change**.
2. On the API task def, set `JWT_SIGN_HASH_SECRET=<that secret's ARN or name>`.
3. Done — the task role's existing grant covers it. (Naming the secret *outside*
   the `cfins-qaworkbench*` prefix would require widening the task-role policy.)

> Startup behavior: a **configured-but-unreadable** secret **fails startup fast**
> (surfaces a broken config immediately, not at first login). If **neither**
> `JWT_SIGN_HASH` nor `JWT_SIGN_HASH_SECRET` is set, the API logs a **warning**
> and token minting fails — treat one of the two as **required**.

---

## 7. ECS Service shape

- **Service** (not a one-shot task): desired count ≥ 2 across AZs, rolling deploys.
- **ALB** → target group on port **8000**, health check path **`/health`**
  (returns `{"status":"ok"}`, no auth, no AWS calls — safe as a liveness probe).
- **Logging:** `awslogs` driver → a CloudWatch log group; keep `LOG_FORMAT=json`
  (each line carries a correlation id + resolved user, queryable in Logs Insights).
- **CPU/mem:** modest — it's an I/O-bound API (e.g. 0.5 vCPU / 1 GB to start), scale
  via task count + target-tracking on CPU/ALB request count.

### Networking / egress
The API task needs outbound reach to: **DynamoDB, S3, Secrets Manager, ECS**
(`run_now`), and — if SSO is on — the **public Cognito** JWKS + userInfo endpoints.
Use private subnets + **NAT** (or VPC endpoints for the AWS services; Cognito needs
internet egress). The ALB is the only inbound path.

---

## 8. Provisioning the AWS resources (already done in dev)

Idempotent boto3 scripts in **`scripts/`** (local-only, excluded from the image)
created the dev resources; they are the reference for what each environment needs:

| Script | Creates |
|---|---|
| `provision_table.py` | DynamoDB `cfins-qaworkbench` (pk/sk + `suite-execution-index` GSI, on-demand) |
| `provision_s3.py` | Per-env artifacts bucket `cfins-qaworkbench-<env>` |
| `provision_ecs.py` | The **runner** cluster/roles/task-def (see the CLI repo doc) |
| `seed_auth.py` | Seed groups (admin/author/viewer) + an admin user |

For prod, DevOps should reimplement these as IaC (CDK/Terraform) with per-env
least-privilege roles. The scripts encode the exact schema/keys/policies to mirror.

---

## 9. Hand-over checklist

Provided by this repo (ready):
- [x] `Dockerfile` — non-root, `linux/amd64`, `:8000`, `/health`, no secrets baked in
- [x] `docs/api-task-role-policy.json` — task-role IAM policy
- [x] `docs/remote-execution-ecs.md` — the `run_now` trigger + config
- [x] `scripts/*` — the resource schemas to reproduce as IaC
- [x] This doc — image + runtime contract

DevOps to build:
- [ ] `.gitlab-ci.yml` — build (`linux/amd64`) → GitLab registry → ECR (immutable tag)
- [ ] ECR repo `cfins-qaworkbench-api` (tag-immutable, scan-on-push)
- [ ] Per-env task defs (env §4, secrets §6, task + execution roles §3)
- [ ] ECS Service + ALB (`/health`, port 8000) + CloudWatch log group
- [ ] Per-env `JWT_SIGN_HASH` secrets + execution-role grants
- [ ] Networking (private subnets + NAT / VPC endpoints)
- [ ] Pin task-def digests for SAT/prod; keep DEV on the rolling family
