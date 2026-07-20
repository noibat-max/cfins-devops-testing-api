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
cd cfins-devops-testing-api
AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \
    ../.venv/bin/python scripts/provision_table.py
```

Env overrides: `WORKBENCH_TABLE` (default `cfins-qaworkbench`), `AWS_REGION`
(default `us-east-1`), `ENABLE_PITR=true` (prod only).

## gen_jwt_secret.py
Prints a random URL-safe HS256 signing secret (48 bytes) as a `JWT_SIGN_HASH=...`
line. No AWS access needed. Copy the output into `.env` yourself.

```bash
python scripts/gen_jwt_secret.py            # print a JWT_SIGN_HASH=... line
```

Env override: `JWT_SIGN_HASH_BYTES` (default `48`).
