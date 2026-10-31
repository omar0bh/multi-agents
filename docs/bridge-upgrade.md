# Cloud-to-Local Bridge Upgrade

The bridge lets the cloud FastAPI service queue work for the user's local PC.  The
local daemon (`scripts/local_bridge.py`) polls the cloud, runs the work locally,
and reports results back.  The upgrade keeps that polling relay architecture and
adds durable tasks, broad local automation, workflow orchestration, security
boundaries, monitoring, recovery and tests.

## Components

| Module | Responsibility |
|---|---|
| `app/bridge/security.py` | Path/command/code policy, protected Windows infrastructure, approval gating, redaction, scrubbed child environment. |
| `app/bridge/runner.py` | Process execution, timeouts, cancellation, output caps, managed background processes, resource/GPU snapshots, interpreter resolution. |
| `app/bridge/context.py` | Execution context, cancellation propagation, throttled progress reporting. |
| `app/bridge/actions.py` | 38 registered local actions plus legacy aliases. |
| `app/bridge/executor.py` | Central dispatch, metadata, audit, history, cancellation integration, legacy fallback. |
| `app/bridge/workflow.py` | DAG workflows: dependencies, parallelism, retries, conditions, verification, checkpoints/resume. |
| `app/bridge/cloud.py` | Extensible local/cloud planner and SSH, Docker/Podman and HTTP providers. |
| `app/bridge/manager.py` | In-memory bridge state, heartbeat leasing, claims, requeue, cancellation, progress, stats. |
| `app/bridge/routes.py` | FastAPI relay endpoints with constant-time auth. |
| `app/storage/task_db.py` | SQLite task store with leases, attempts, progress, cancellation, priority. |
| `app/bridge/audit.py` | JSONL audit log and SQLite execution history/idempotency cache. |
| `app/bridge/capabilities.py` | Capability manifest advertised during heartbeat. |

## Task lifecycle

1. Cloud code calls `bridge_manager.queue_task(...)` or the daemon receives a task.
2. `POST /api/bridge/heartbeat` claims up to `limit` pending tasks with a lease.
3. The daemon executes the task through `bridge_executor.execute(...)`.
4. Progress is streamed with `POST /api/bridge/task/progress`.
5. The terminal result is reported with `POST /api/bridge/task/report`.
6. Leases are renewed through heartbeats; expired leases are requeued or failed after `max_attempts`.
7. `POST /api/bridge/task/{id}/cancel` cancels pending tasks immediately or signals running tasks.

## Built-in actions

Categories include diagnostics, shell, code execution, environment/package
installation, files, archives, process management, HTTP/API calls, git,
orchestration and cloud routing.  Legacy actions such as `create_project`,
`screenshot_analyze` and desktop helpers remain available through the fallback
handler in `scripts/local_bridge.py`.

## Workflows

A workflow is submitted with the `workflow` action and a `steps` list.  Each step
has an `action`, `params`, optional `depends_on`, `when`, `retries`,
`retry_delay`, `timeout`, `verify`/`expect`, `continue_on_error` and template
references such as `{{ steps.previous.stdout }}`.

Supported behaviour:

- Dependency graphs with cycle detection.
- Parallel execution up to `max_parallel`.
- Conditional skips (`when: false`).
- Retry on failure.
- Result verification by exit code, stdout/stderr, JSON, HTTP status and files.
- Checkpoint files under `BRIDGE_WORKFLOW_DIR`.
- Resume with `resume: true`; only succeeded steps are reused.
- Cancellation through the shared execution context.

## Cloud routing

`plan_execution` evaluates local capacity against requirements and configured
cloud providers.  Providers are loaded from `BRIDGE_CLOUD_PROVIDERS` (JSON list
or object).  Built-in types are `ssh`, `docker`/`podman` and `http`.  New
providers can be registered at runtime by implementing `CloudProvider`.

Selection considers CPU, memory, disk, GPU, availability, cost, explicit provider
preference and priority (`capability`, `cost`, `speed`).

## Security model

Normal user-level development work is allowed in the home directory, project root
and temp directory.  Destructive operations outside those roots require
`BRIDGE_APPROVAL_TOKEN` to be configured and supplied as `approval_token`.

Critical Windows infrastructure is never modified, even with a valid approval
token.  Protected targets include `C:\Windows`, `C:\Boot`, `C:\EFI`,
`C:\Recovery`, `C:\System Volume Information`, page/hibernation files, drivers,
registry hives, hosts/networks files, Defender and firewall tampering patterns,
UAC bypass patterns, encoded PowerShell, raw disk writes and credential hives
(SAM, SECURITY, SYSTEM, SOFTWARE, Protect).

Command inspection decodes PowerShell encoded commands, scans shell chaining and
redirection, and inspects interpreter/code-like commands so `python -c` or
PowerShell cannot bypass protected paths.  Archive extraction blocks path
traversal and archive creation requires read permission for every member.

Child processes receive a scrubbed environment.  Secrets are redacted from
results, logs and history.

## Environment variables

LLM and Telegram variables remain unchanged.  Bridge variables:

```dotenv
BRIDGE_SECRET=
BRIDGE_APPROVAL_TOKEN=
BRIDGE_ALLOWED_ROOTS=
BRIDGE_MAX_COMMAND_CHARS=32768

BRIDGE_AUDIT_LOG=data/bridge_audit.jsonl
BRIDGE_HISTORY_DB=data/bridge_history.db
BRIDGE_WORKFLOW_DIR=data/bridge_workflows
BRIDGE_CLOUD_PROVIDERS=

BRIDGE_POLL_INTERVAL=5
BRIDGE_MAX_PARALLEL_TASKS=4
BRIDGE_LEASE_SECONDS=90
BRIDGE_REPORT_ATTEMPTS=5
BRIDGE_LOG_MAX_BYTES=5242880
```

## Relay endpoints

- `GET /api/bridge/status`
- `POST /api/bridge/heartbeat`
- `POST /api/bridge/task/progress`
- `POST /api/bridge/task/report`
- `GET /api/bridge/tasks`
- `GET /api/bridge/task/{task_id}`
- `POST /api/bridge/task/{task_id}/cancel`
- `POST /api/bridge/task/cancel`
- `GET /api/bridge/capabilities`

All endpoints require `X-Bridge-Secret` when `BRIDGE_SECRET` is configured.

## Local daemon

Run:

```powershell
.\.venv\Scripts\python.exe scripts\local_bridge.py
```

The daemon performs capability heartbeats, claims work, renews leases, streams
progress, reports results with retries, polls cancellation requests, rotates
logs and falls back to legacy handlers for actions not in the upgraded registry.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Current result: `135 passed, 1 warning`.

Focused suites:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_bridge_security.py tests/test_bridge_runner.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_bridge_actions.py tests/test_bridge_workflow.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_bridge_cloud.py tests/test_bridge_taskdb.py tests/test_bridge_api_upgrade.py -q
```

The security suite includes protected-path modification attempts, critical shell
commands, code bypass attempts, shell chaining, approval validation and
redaction.  The full suite also covers workflow checkpoints/resume, cancellation,
task leases, API auth and managed processes.
