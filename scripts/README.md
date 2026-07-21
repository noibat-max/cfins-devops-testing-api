# scripts/ — provisioning & seed tooling

Local-only utilities that create/seed the AWS resources the API uses. **Not**
part of the deployed image (excluded via `.dockerignore`). They run against
**real AWS** using the `cfins-local` profile (boto3 default credential chain).

> ⚠️ These create real (billable) resources. On-demand DynamoDB is ~$0 at rest,
> but always know what you're running.

## provision_table.py
Creates the single DynamoDB table `cfins-qaworkbench` (single-table design:
`pk`/`sk` + `suite-execution-index` GSI, on-demand billing). Idempotent.

```bash
cd qa-platform-api
AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \
    ../.venv/bin/python scripts/provision_table.py
```

Env overrides: `WORKBENCH_TABLE` (default `cfins-qaworkbench`), `AWS_REGION`
(default `us-east-1`), `ENABLE_PITR=true` (prod only).

## gen_jwt_secret.py
Prints a random URL-safe HS256 signing secret (48 bytes) as a `JWT_SIGN_HASH=...`
line. No AWS access needed. Copy the output into `.env` yourself — this is the
**local** path (inline key).

```bash
python scripts/gen_jwt_secret.py            # print a JWT_SIGN_HASH=... line
```

Env override: `JWT_SIGN_HASH_BYTES` (default `48`).

## provision_jwt_secret.py
Creates the HS256 signing key in **AWS Secrets Manager**, one per environment, at
`<SECRET_PREFIX>/<ENVIRONMENT>/jwt-sign-hash` (e.g. `cfins-qaworkbench/dev/jwt-sign-hash`).
This is the **deployed** path: set `JWT_SIGN_HASH_SECRET` to the printed name and
the API fetches it at startup via its task role (see `DEPLOYMENT.md` §6). The
secret value never leaves AWS (not printed). Idempotent — an existing secret is
left untouched unless `SECRET_FORCE=true` (rotates the value).

```bash
AWS_PROFILE=cfins-local AWS_REGION=us-east-1 ENVIRONMENT=dev \
    ../.venv/bin/python scripts/provision_jwt_secret.py
```

Env: `SECRET_PREFIX` (default `cfins-qaworkbench`), `ENVIRONMENT` (default `local`),
`JWT_SECRET_NAME` (full-name override), `JWT_SIGN_HASH_BYTES` (default `48`),
`SECRET_FORCE=true` (rotate an existing secret).
