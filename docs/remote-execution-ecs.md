# Remote Execution (Run Now → ECS Fargate) — Ops & Hand-off

This documents the **remote execution** path — a user clicks **Run Now** in the UI
(or a suite's Run Now), the API launches a one-shot **Fargate task** that runs the
test headless and writes results straight to DynamoDB/S3 via its task role. It is
the reference for DevOps to **productionize** (CI/CD, multi-env, IaC). Everything
below was **built and verified end-to-end in the dev account** (`103930328611`,
`us-east-1`) using the scoped `cfins-local` IAM user.

There are two run paths; this doc is **only** the remote one:
- **Local** (`mode=local`) — the tester's CLI (`qa nova run` / `run-suite`) runs the browser.
- **Remote** (`mode=run_now`) — the API calls `ecs.run_task`; **this document.**

---

## 1. Flow

```
UI "Run Now"  ─POST /api/nova/usecase/{id}/execute {mode:"run_now", capture}─►  API
   (or suite)                                                                    │
                                    creates the execution record(s)              │
                                    ecs.run_task(cluster, taskDef, overrides:     │
                                      USECASE_ID, EXECUTION_ID, CAPTURE)  ────────┼──► Fargate task
                                                                                 │      (worker image)
   UI polls execution/roll-up  ◄────────────────────────────────────────────────┘      pulls image (ECR)
                                                                                        runs headless (Nova Act)
   task role → DynamoDB (status/steps) + S3 (artifacts) + Secrets Manager (secrets)
```

A **suite** run_now creates one execution per member and launches **one Fargate task
per member** (they run in parallel); the suite status is a read-live roll-up over
those member executions (via the `suite-execution-index` GSI).

---

## 2. Components & where they live

| Component | Where | Notes |
|---|---|---|
| Runner **image** | `cfins-devops-testing-cli/Dockerfile` | Engine + `python -m qa_cli.worker` (CMD) + headless Chromium + boto3. Build once, **region/env-agnostic**, **no creds/keys baked in.** |
| **ECR** repo | `cfins-qaworkbench-runner` | Immutable tags, scan-on-push. |
| **ECS** infra | `cfins-devops-testing-api/scripts/provision_ecs.py` | Idempotent: task-exec role, task role, log group, cluster, task def. |
| API **trigger** | `app/routers/nova/executions.py::_launch_ecs_task` + `execute` (mode `run_now`); suites `app/routers/nova/suites.py::execute_test_suite` | Reads ECS config from env; calls `ecs.run_task`. |
| API **config** | `app/config.py` (ECS_* / RUNNER_* + `ecs_enabled`) | Per-environment env vars (see §5). |

---

## 3. The image (build once, promote the digest)

Built from `cfins-devops-testing-cli/Dockerfile`. Must be **`linux/amd64`** (Fargate x86).

```bash
cd cfins-devops-testing-cli
docker build -t cfins-qaworkbench-runner:dev .           # x86 host builds amd64 natively
# Apple Silicon: docker buildx build --platform linux/amd64 -t cfins-qaworkbench-runner:dev --load .

REG=103930328611.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REG
docker tag  cfins-qaworkbench-runner:dev $REG/cfins-qaworkbench-runner:<version>
docker push $REG/cfins-qaworkbench-runner:<version>
```

- **Immutable tags** are on → each push needs a **new** tag (semver or git SHA).
- The task def **pins the digest** (`repo@sha256:…`), resolved by `provision_ecs.py`.
- **Note:** the dev image `0.1.0` in ECR predates the Dockerfile's
  `NOVA_ACT_SKIP_PLAYWRIGHT_INSTALL=1` ENV. That flag is currently also injected via
  the task-def env by `provision_ecs.py`, so `0.1.0` works. **Production images built
  from the current Dockerfile bake the flag in** — the task-def injection is then
  belt-and-suspenders. (Nova Act would otherwise try `playwright install` at runtime
  and fail as the non-root user; the browser is already baked into the image.)

---

## 4. IAM (three distinct identities)

1. **Task-execution role** `cfins-qaworkbench-runner-exec` — ECS uses it to **pull the
   image + write logs**. Managed policy `AmazonECSTaskExecutionRolePolicy`, plus (when a
   Nova Act key secret is wired) `secretsmanager:GetSecretValue` on **just that secret**.
2. **Task role** `cfins-qaworkbench-runner-task` — what the **worker** uses at runtime.
   Inline `workbench-access`: scoped `cfins-qaworkbench*` DynamoDB (+ GSI), S3, Secrets.
   No creds are baked in — boto3's default chain resolves this role in the container.
3. **The API's own identity** — needs `ecs:RunTask` + `iam:PassRole` (on the two runner
   roles, conditioned to `ecs-tasks.amazonaws.com`). In dev the API runs as `cfins-local`,
   which was granted these via the inline policy **`cfins-local-ecs-provisioning`** (that
   policy also grants the ECR/ECS/IAM-create perms used to *provision* — DevOps should
   split "provision" vs "run" perms in prod; the API only needs RunTask + PassRole).

---

## 5. API config (per environment — the "which cluster/task" answer)

The UI/CLI hold **none** of this — the API resolves everything from its own env:

| Env var | Example | Required |
|---|---|---|
| `ECS_CLUSTER` | `cfins-qaworkbench` | yes |
| `RUNNER_TASK_DEFINITION` | `cfins-qaworkbench-runner` (family = latest) or `…:3` (pinned) | yes |
| `RUNNER_SUBNETS` | `subnet-…,subnet-…` | yes |
| `RUNNER_SECURITY_GROUPS` | `sg-…` | yes |
| `RUNNER_LAUNCH_TYPE` | `FARGATE` | default FARGATE |
| `RUNNER_ASSIGN_PUBLIC_IP` | `ENABLED` | default ENABLED |
| `RUNNER_CAPTURE` | `screenshots` \| `full` | default screenshots (per-run `capture` overrides) |

`ecs_enabled` = cluster + task-def + subnets all set; when false, `run_now` returns a
clear **400** (local runs still work).

---

## 6. Per-environment & promotion/rollback

A **task-definition revision bundles {image digest + env vars + roles}** — it's the
promotion **and** rollback unit.

- **DEV:** `RUNNER_TASK_DEFINITION` = bare family → uses the latest revision (fast iteration).
- **SAT/prod:** **pin a revision** (`…:3`) so a new registration can't silently change
  what runs. Promote = point config at a vetted revision; roll back = point back.
- **Recommended for prod:** store the "active" task-def ARN in an **SSM Parameter** and
  have the API read it → move the pointer to roll back **without a redeploy**. (ECS task
  defs have no movable `:LATEST` alias; the family name = latest-active only.)

---

## 7. Nova Act key

Stored as a Secrets Manager secret (dev: `cfins-qaworkbench/nova-act-key`). Wire it by
running `provision_ecs.py` with `NOVA_ACT_SECRET_ARN=<arn>` — it adds the secret to the
task def's `secrets` (injected as `NOVA_ACT_API_KEY`) and grants the **exec role** read on
just that secret. The key is **never** in the image or in DynamoDB.

---

## 8. Networking

Dev uses the **default VPC** public subnets + default SG + `assignPublicIp=ENABLED` (the
task needs egress to pull from ECR and reach Nova Act + the target site).
**Prod recommendation:** private subnets + **NAT** (or VPC endpoints for ECR/S3/DynamoDB/
Secrets/Logs) instead of public IPs; a dedicated egress-only SG.

---

## 9. What DevOps productionizes (out of scope for this effort)

- **CI/CD**: build the `linux/amd64` image, push with an immutable tag, scan; IaC (CDK/
  Terraform) for ECR/roles/cluster/task-defs instead of the dev `provision_ecs.py`.
- **Multi-env**: per-env task defs (pinned digests), roles, clusters; the SSM pointer.
- **Secrets**: per-env Nova Act key secret.
- **Guardrails**: task `stopTimeout` / an execution watchdog so a hung run can't bill
  forever; CloudWatch alarms; log retention; least-privilege split of provision-vs-run IAM.
- **Deferred features**: `queued` (SQS) + `scheduled` (EventBridge) modes; **Bedrock
  AgentCore Browser** (managed browser — today it's headless Chromium in-container); DCV
  live view.

---

## 10. Verified in dev (what "done" means here)

- **Single use case** `run_now` → Fargate task green: steps passed, screenshots in S3,
  shown in Execution History (`trigger=ui`, `mode=run_now`).
- **Full capture** `run_now` green: screenshots + Nova Act HTML/JSON traces + a video
  (webm), all in S3.
- **Suite** `run_now` green: 2 members → **2 parallel Fargate tasks**, roll-up
  pending→running→completed (2/2).
- Task role did all DynamoDB/S3/Secrets work; **no credentials in the image**.
