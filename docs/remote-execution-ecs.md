# The API on ECS + the Run Now trigger (Ops & Hand-off)

This is the **API side** of remote execution: how the **`cfins-devops-testing-api`**
service is deployed to ECS, and how its **`run_now`** mode launches a runner task via
`ecs.run_task`. The **runner** (the worker image the task runs, its ECS task
definition/roles, and how they're built/provisioned) is documented in the CLI repo:
**`cfins-devops-testing-cli/docs/runner-ecs.md`** — this doc does not repeat it.

Verified in the dev account (`103930328611`, `us-east-1`) via the scoped `cfins-local` user.

---

## 1. Flow (API's part)

```
UI "Run Now"  ─POST /api/nova/usecase/{id}/execute {mode:"run_now", capture}─►  API (ECS Service)
   (or suite)                                                                     │
                                    creates the execution record(s)               │
                                    ecs.run_task(cluster, taskDef, overrides:      │
                                      USECASE_ID, EXECUTION_ID, CAPTURE)  ─────────┼──► runner task
   UI polls execution/roll-up  ◄─────────────────────────────────────────────────┘     (see CLI repo doc)
```

The API **creates the execution record and calls `ecs.run_task`** — that's its whole role
in a remote run. It does not run the browser or write artifacts; the runner task does
(directly, via its own task role). A **suite** `run_now` launches **one task per member**
(parallel). Code: `app/routers/nova/executions.py::_launch_ecs_task` (+ `execute`) and
`app/routers/nova/suites.py::execute_test_suite`.

---

## 2. Deploying the API to ECS

The API is a **long-lived ECS Service** behind an **ALB** (health check on `/health`),
unlike the runner's one-shot tasks. Image = the API's own container (`Dockerfile` in this
repo → e.g. ECR repo `cfins-qaworkbench-api`; build/push in the repo README). Non-root,
uvicorn on `:8000`, config from the task-def environment (nothing baked in).

---

## 3. IAM — the API's task role

On ECS the API's boto3 code assumes **its own task role** (resolved via the
container-credentials endpoint; no profile/keys). It needs the app's runtime perms **plus**
the `run_now` trigger perms:

- `cfins-qaworkbench` DynamoDB (+ `index/*` for the `suite-execution-index` GSI)
- `cfins-qaworkbench-*` S3 (the presigned URLs it mints are signed with these creds)
- `cfins-qaworkbench*` Secrets Manager (per-use-case secrets) + `ListSecrets`
- **`ecs:RunTask`** on `cfins-qaworkbench-runner:*` **+** **`iam:PassRole`** on
  `cfins-qaworkbench-runner-*` (condition `PassedToService=ecs-tasks.amazonaws.com`) — this
  is what lets the API launch and hand the runner roles to a runner task.

Full policy: **`docs/api-task-role-policy.json`**. Create the role (standard ecs-tasks trust)
and attach it:

```bash
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam create-role --role-name cfins-qaworkbench-api-task \
  --assume-role-policy-document "$TRUST" --region us-east-1 --profile cfins-local
aws iam put-role-policy --role-name cfins-qaworkbench-api-task \
  --policy-name workbench-api-access \
  --policy-document file://docs/api-task-role-policy.json --region us-east-1 --profile cfins-local
```

The API's task also needs a **task-execution role** (ECR pull + logs — the managed
`AmazonECSTaskExecutionRolePolicy`).

> In **dev** the API runs **locally** as `cfins-local` (boto3 default chain), which already
> has these perms — so the task role isn't exercised until the API is deployed to ECS.
> `cfins-local` also holds provision-time ECR/ECS/IAM-create perms
> (`cfins-local-ecs-provisioning`); in prod, DevOps should split "provision" (CI/CD) from
> "run" (this task role) into separate identities.

---

## 4. API config (per environment — the "which cluster/task" answer)

The UI/CLI hold **none** of this — the API resolves everything from its own env
(`app/config.py`). These are **API deploy-time config** that point at the runner infra:

| Env var | Example | Required |
|---|---|---|
| `ECS_CLUSTER` | `cfins-qaworkbench` | yes |
| `RUNNER_TASK_DEFINITION` | `cfins-qaworkbench-runner` (family = latest) or `…:3` (pinned) | yes |
| `RUNNER_SUBNETS` | `subnet-…,subnet-…` | yes |
| `RUNNER_SECURITY_GROUPS` | `sg-…` | yes |
| `RUNNER_LAUNCH_TYPE` | `FARGATE` | default FARGATE |
| `RUNNER_ASSIGN_PUBLIC_IP` | `ENABLED` | default ENABLED |
| `RUNNER_CAPTURE` | `screenshots` \| `full` | default screenshots (per-run `capture` overrides) |

`ecs_enabled` = cluster + task-def + subnets all set; when false, `run_now` returns a clear
**400** (local runs still work). The **values** for the cluster/task-def/subnets come from
the runner infra — see the CLI repo doc.

---

## 5. Per-environment & promotion/rollback

The runner **task-definition revision bundles {image digest + env + roles}** — it's the
promotion/rollback unit. The API selects it via `RUNNER_TASK_DEFINITION`:

- **DEV:** bare family → latest revision (fast iteration).
- **SAT/prod:** **pin a revision** (`…:3`) so a new registration can't silently change what
  runs. Promote = point config at a vetted revision; roll back = point back.
- **Recommended for prod:** store the active task-def ARN in an **SSM Parameter** and have
  the API read it → move the pointer to roll back **without a redeploy**. (ECS task defs have
  no movable `:LATEST` alias.)

---

## 6. What DevOps productionizes (API side)

- **CI/CD** for the **API image** (build `linux/amd64`, immutable tag, scan) and the ECS
  **Service** (ALB, target group `/health`, desired count, autoscaling) via IaC.
- Per-env API task defs with the **API task role** (§3) + exec role, and the ECS/RUNNER_*
  config (§4) as env.
- The **SSM movable pointer** for `RUNNER_TASK_DEFINITION`.
- Split provision-vs-run IAM.

(Runner-side productionization — the runner image CI/CD, its roles/cluster/task-defs — is in
the CLI repo doc.)

---

## 7. Verified in dev

- **API-triggered `run_now`**: `POST …/execute {mode:"run_now"}` → the API called
  `ecs.run_task` → Fargate task → execution polled **pending→executing→completed**
  (`trigger=ui`, `mode=run_now`), steps passed, screenshots in S3; viewer blocked by the
  execute scope.
- **Suite `run_now`**: 2 members → **2 parallel tasks** launched by the API, roll-up green.
- Full-capture + the runner internals are covered in the CLI repo doc.
