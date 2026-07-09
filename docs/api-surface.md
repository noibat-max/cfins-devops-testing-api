# API Surface — sample-qa-studio → cfins-devops-testing-api

Extracted from the pinned sample (`v1.1.0` / `960d6fd`):
`web-app/lib/api-stack.ts` (route table) + `web-app/lambdas/endpoints/*` (handlers,
which declare their required scope via `require_scopes(event, [...])`).

**Totals:** 105 API Gateway routes across 84 resources (GET 42 · POST 37 · DELETE 12 · PATCH 11 · PUT 3).
Every route is authorized by the Cognito Lambda authorizer + a per-handler scope check.

This is the **parity checklist**: each row becomes a FastAPI route in `app/routers/`.
Legend: **IN** = port for parity · **OUT** = excluded by a recorded decision ·
**PROD** = prod-only, stub locally.

Scopes in our model (seeded): `usecases.read/write/execute`, `templates.read/write`,
`executions.read/write`, `suite.read/write`, `api/admin` (inherits all). The sample's
`oauth-clients.*` scopes are **dropped** (see OAuth Clients below).

---

## 1. Use cases — CRUD  ·  IN  (scope: `usecases.*`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| GET | /usecases | list_usecases | usecases.read |
| POST | /usecase | create_usecase | usecases.write |
| GET | /usecase/{id} | get_usecase | usecases.read |
| PATCH | /usecase/{id} | update_usecase | usecases.write |
| DELETE | /usecase/{id} | delete_usecase | usecases.write |
| GET | /usecase/{id}/export | export_usecase | usecases.read |
| POST | /usecase/{id}/clone | clone_usecase | usecases.write |
| POST | /import | import_usecase | usecases.write |

> `create_usecase` carries ANDROID/IOS platform params — **mobile fields OUT** (Device Farm excluded); port the web/browser path only.

## 2. Steps  ·  IN  (scope: `usecases.*`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /usecase/{id}/steps | create_step | usecases.write |
| GET | /usecase/{id}/steps | list_steps | usecases.read |
| PATCH | /usecase/{id}/steps/{stepId} | update_step | usecases.write |
| DELETE | /usecase/{id}/steps/{stepId} | delete_step | usecases.write |
| PATCH | /usecase/{id}/steps/reorder | reorder_steps | usecases.write |
| POST | /usecase/{id}/steps/{stepId}/update-from-template | update_step_from_template | usecases.write |

## 3. Use-case config — variables · hooks · headers · secrets  ·  IN  (scope: `usecases.*`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /usecase/{id}/variables | create_usecase_variables | usecases.write |
| GET | /usecase/{id}/variables | get_usecase_variables | usecases.read |
| POST | /usecase/{id}/hooks | create_usecase_hooks | usecases.write |
| GET | /usecase/{id}/hooks | get_usecase_hooks | usecases.read |
| POST | /usecase/{id}/headers | create_usecase_headers | usecases.write |
| GET | /usecase/{id}/headers | get_usecase_headers | usecases.read |
| POST | /usecase/{id}/secrets | create_usecase_secrets | usecases.write |
| GET | /usecase/{id}/secrets | get_usecase_secrets | usecases.read |
| PATCH | /usecase/{id}/secrets | update_usecase_secrets | usecases.write |
| DELETE | /usecase/{id}/secrets | delete_usecase_secrets | usecases.write |
| GET | /usecase/{id}/secrets/{secret_key}/value | get_usecase_secret_value | usecases.read |

> Secrets values live in **Secrets Manager** (`cfins-qaworkbench-*`); the IAM policy already scopes it.

## 4. Templates linkage + subscriptions  ·  IN
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /usecase/{id}/import-template | import_template | usecases.write |
| GET | /usecase/{id}/template-updates | check_template_updates | templates.read |
| GET | /usecase/{id}/subscription | get_usecase_subscription | usecases.read |
| POST | /usecase/{id}/subscription | subscribe_usecase | usecases.write |
| DELETE | /usecase/{id}/subscription | unsubscribe_usecase | usecases.write |

## 5. Execution engine (per use case)  ·  IN  (scope: `executions.*`; live-view PROD)
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /usecase/{id}/execute | execute_usecase | **execute** (user+M2M; SQS→worker) |
| GET | /usecase/{id}/executions | list_executions | executions.read |
| GET | /usecase/{id}/executions/{executionId} | get_execution | executions.read |
| DELETE | /usecase/{id}/executions/{executionId} | delete_execution | executions.write |
| PATCH | /usecase/{id}/executions/{executionId}/status | update_execution_status | executions.write (worker callback) |
| POST | /usecase/{id}/executions/{executionId}/stop | stop_execution | executions.write |
| GET | /usecase/{id}/executions/{executionId}/steps | list_execution_steps | executions.read |
| GET | /usecase/{id}/executions/{executionId}/steps/{stepId} | get_execution_step | executions.read |
| PATCH | …/steps/{stepId}/status | update_execution_step_status | executions.write (worker) |
| GET | …/steps/{stepId}/trace | get_step_trace | executions.read |
| GET | /usecase/{id}/executions/{executionId}/variables | get_execution_variables | executions.read |
| GET | /usecase/{id}/executions/{executionId}/live-view | get_live_view | executions.read · **PROD (DCV)** |
| GET | /usecase/{id}/executions/{executionId}/video | get_video_playback | executions.read |
| GET | /usecase/{id}/executions/{executionId}/downloads | list_downloads | executions.read |
| GET | …/downloads/{fileName} | download_file | executions.read |
| GET | /usecase/{id}/executions/{executionId}/events | list_recording_batches | executions.read |
| GET | …/event/{batchId} | get_recording_batch | executions.read |
| POST | /usecase/{id}/executions/{executionId}/download-recording | request_recording_download | executions.write |
| POST | /usecase/{id}/executions/{executionId}/artifacts | generate_execution_artifact_url | executions.write |
| GET | /usecase/{id}/executions/{executionId}/artifacts | list_execution_artifacts | executions.read |
| PATCH | …/artifacts/{artifactId} | confirm_artifact_upload | executions.write |
| POST | …/steps/{stepId}/artifacts | generate_step_artifact_url | executions.write |

> Artifacts (video/screenshots/traces) live in **S3** (`cfins-qaworkbench-*`); execution is queued to **SQS** and run by the **worker** (kept). `live-view` streams via **DCV** → prod-only; stub locally.

## 6. Scheduling  ·  IN  (EventBridge Scheduler)
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /usecase/{id}/schedule | create_schedule | usecases.write |
| GET | /usecase/{id}/schedule | get_schedule | usecases.read |
| DELETE | /usecase/{id}/schedule | delete_schedule | usecases.write |
| PUT | /test-suites/{suite_id}/schedule | update_suite_schedule | suite.write |

## 7. Templates  ·  IN  (scope: `templates.*`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| GET | /templates | list_templates | templates.read |
| POST | /templates | create_template | templates.write |
| GET | /templates/{id} | get_template | templates.read |
| PATCH | /templates/{id} | update_template | templates.write |
| DELETE | /templates/{id} | delete_template | templates.write |
| GET | /templates/{id}/steps | list_template_steps | templates.read |
| POST | /templates/{id}/steps | create_template_step | templates.write |
| PATCH | /templates/{id}/steps/reorder | reorder_template_steps | templates.write |
| PATCH | /templates/{id}/steps/{stepId} | update_template_step | templates.write |
| DELETE | /templates/{id}/steps/{stepId} | delete_template_step | templates.write |
| GET | /templates/{id}/variables | get_template_variables | templates.read |
| POST | /templates/{id}/variables | create_template_variables | templates.write |
| POST | /templates/{id}/apply | apply_template | templates.read + usecases.write |

## 8. Test suites  ·  IN  (scope: `suite.*`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| GET | /test-suites | list_test_suites | suite.read |
| POST | /test-suites | create_test_suite | suite.write |
| GET | /test-suites/{suite_id} | get_test_suite | suite.read |
| PUT | /test-suites/{suite_id} | update_test_suite | suite.write |
| DELETE | /test-suites/{suite_id} | delete_test_suite | suite.write |
| GET | /test-suites/{suite_id}/usecases | list_suite_usecases | suite.read |
| POST | /test-suites/{suite_id}/usecases | add_usecases_to_suite | suite.write |
| DELETE | /test-suites/{suite_id}/usecases/{usecase_id} | remove_usecase_from_suite | suite.write |
| POST | /test-suites/{suite_id}/execute | execute_test_suite | suite.write |
| GET | /test-suites/{suite_id}/executions | list_suite_executions | suite.read |
| GET | /test-suites/{suite_id}/executions/{execution_id} | get_suite_execution | suite.read |
| PATCH | …/executions/{execution_id}/status | update_suite_execution_status | suite.write (worker) |
| POST | …/executions/{execution_id}/artifacts | generate_suite_artifact_url | suite.write |
| GET | …/executions/{execution_id}/artifacts | list_suite_artifacts | suite.read |

> Suite executions use the `suite-execution-index` GSI already provisioned on the table.

## 9. AI test generation  ·  IN  (Bedrock)
| Method | Path | Handler | Scope |
|---|---|---|---|
| GET | /models | list_models | (any authenticated) |
| POST | /generate-usecase | generate_usecase | usecases.write |

> Bedrock IN scope; `generate-usecase` can take up to 120s (sample notes the API GW 29s timeout limit — not our constraint on uvicorn).

## 10. Users — Administration  ·  IN  (scope: `api/admin`)
| Method | Path | Handler | Scope |
|---|---|---|---|
| GET | /users | list_users | admin |
| POST | /users | create_user (add_user) | admin |
| GET | /users/{username} | get_user | admin |
| DELETE | /users/{username} | delete_user (remove_user) | admin |
| PUT | /users/{username}/groups | update_user_groups | admin |

> Sample manages **Cognito** users → needs `cognito-idp` perms (not in `cfins-local` policy yet). Our local users live in DynamoDB (`pk=USERS`); the admin screen manages both providers later.

## 11. S3 utility  ·  IN
| Method | Path | Handler | Scope |
|---|---|---|---|
| POST | /generate-s3-url | generate_s3_url | (authenticated; presign) |

---

## OUT of scope (recorded decisions)
| Group | Routes | Why |
|---|---|---|
| **Devices (Device Farm)** | GET /devices (list_device_farm_devices) | Mobile OUT |
| **OAuth Clients** | GET/POST /oauth-clients, DELETE /oauth-clients/{clientId}, POST …/rotate-secret, GET /scopes | M2M OAuth-client screen **dropped**; `oauth-clients.*` scopes not in our model |
| **Wizard / Recorder** | POST /wizard/start, …/{sessionId}/step, /accept/{stepId}/{usecaseId}, /reject/…, /restart, /terminate/{usecaseId}, /command, GET …/recording (8 routes) | Server side of the **chrome-extension Recorder** — chrome-extension dropped entirely |
| **Device Farm recording** | download_device_farm_recording (not a routed endpoint) | Mobile OUT |

## Not API routes (internal / worker callbacks)
`handle_task_state_change`, `update_usecase_last_execution`, `send_notification`,
`process_wizard_command`, `build_cache`, `lambda_init`, `url_override`, `variable_merge`,
`utils`, `recording_models` — worker/Step-Functions/helpers, not in `api-stack.ts`.

## Cross-cutting (ours, not in the sample's table)
- `GET /health` — done
- `POST /auth/login`, `GET /auth/me` — done (local + Cognito SSO)
- `GET /apps` — done (hardcoded catalog)

---

## Suggested porting sequence
1. **Use cases CRUD** (§1) + **Steps** (§2) — the spine; `usecases.*` scopes already enforced.
2. **Templates** (§7) — self-contained; feeds §1 via apply/import.
3. **Test suites** (§8) — groups use cases; uses the GSI.
4. **Execution engine** (§5) + **worker/SQS/S3** — the heaviest slice; brings runs + artifacts.
5. **Scheduling** (§6), **AI generation** (§9), **config/secrets** (§3), **subscriptions** (§4).
6. **Admin — Users** (§10) — needs `cognito-idp` policy additions.

**Deferred (post-parity):** C&F step types `approval` / `reconcile` / `data`.
