# Isolated durable coordination worker

Core's `/coord/tasks` API and `orchestrator_tasks` remain the only business ledger.
DBOS records execution, retry and recovery history in a **separate PostgreSQL
database**. A DBOS `SUCCESS` means the worker submitted evidence for review; only
an independent Core session can accept it as `verified_done`.

The only handler in this release is `workspace_snapshot`. It reads a task's
explicit Git worktree and publishes a JSON artifact. It cannot execute task text,
shell commands, deployment instructions or a different handler. Git commands are
fixed, use no shell and no optional index writes. Repository fsmonitor and
clean/smudge/process filters are disabled during inspection.
Submodule contents are deliberately not inspected (`--ignore-submodules=all`),
so a submodule's own configuration cannot launch a nested process filter. The
artifact records `submodules_inspected: false`.

## Installation and configuration

Use Python 3.12 and a separate venv. Do not install DBOS into the Core API venv.

```sh
python3.12 -m venv .venv-coordination
.venv-coordination/bin/python -m pip install -r requirements-coordination.txt
```

On Windows the executable is `.venv-coordination/Scripts/python.exe`. Run all
commands from the repository root. This venv is local runtime data; never stage it
in Git. `requirements-coordination.txt` pins DBOS 3.5.0; the API has no DBOS import.

Provide configuration through the worker's environment or a service-owned,
mode-0600 systemd `EnvironmentFile`. The CLI does not accept a key in arguments or
JSON. Obtain credentials through the owner's existing credential process.

| Variable | Meaning |
| --- | --- |
| `COGCORE_COORD_API_URL` | Core origin, e.g. `https://mcp.me-ai.ru`; HTTP allowed only on loopback |
| `COGCORE_COORD_API_KEY` | Owner-scoped worker key; `COGCORE_API_KEY` is the fallback |
| `COGCORE_COORD_DB_URL` | Dedicated DBOS PostgreSQL URL; no implicit SQLite fallback |
| `COGCORE_COORD_AGENT_ID` | Agent identity belonging to the worker key |
| `COGCORE_COORD_SESSION_ID` | Stable worker session, preserved across process restarts |
| `COGCORE_COORD_EXECUTOR_ID` | Stable DBOS executor identity, unique to this worker installation |
| `COGCORE_COORD_ALLOWED_ROOTS` | JSON array of existing absolute allowed directories, e.g. `["/srv/cogcore-worktrees"]` |
| `COGCORE_COORD_ARTIFACTS_DIR` | Existing absolute directory writable only by the service account |
| `COGCORE_COORD_CONCURRENCY` | Global/per-executor limit for this worker's queue, default 1, maximum 8 |
| `COGCORE_COORD_LEASE_SECONDS` | Core lease, default 300, range 120–3600 |
| `COGCORE_COORD_RECONCILE_SECONDS` | Running-attempt scan interval, default 15 |
| `COGCORE_COORD_PROJECT` | Optional project filter for recovery scans |
| `COGCORE_COORD_APP_NAME` | Stable DBOS application, default `cogcore-coordination` |
| `COGCORE_COORD_APP_VERSION` | Stable workflow code version, default `workspace-snapshot-v1` |

The stable session, executor, app version, database and artifact directory must
survive restart. Do not reuse this executor/session for a different live process
or a different host. Each worker has a queue name derived from agent/session, so
another host cannot dequeue its local-path work by accident. Do not change the
workflow version while unfinished executions depend on it: drain/reconcile those
executions with the matching code first. Later code versions must get their own
explicit application version.
DBOS 3.5.0 requires application version names to be unique across different
applications sharing one system database. If overriding `APP_NAME`, supply a
distinct stable `APP_VERSION` too; repeatable tests generate both together.

The service unit is a template, not an installation action. Create the dedicated
account and directories, grant read access only to approved worktrees, populate
the credential environment, and check the unit paths before enabling it.
`ProtectSystem=strict` permits writes only under the configured artifact path.
DBOS's first launch creates/migrates its dedicated technical schema; run it only
against the explicitly prepared DBOS database, with the necessary permission.

## Core CLI and worker commands

The Core client uses only the Python standard library:

```sh
python -m tools.coordination_client list --all --project cognitive_core
python -m tools.coordination_client get TASK_UUID
python -m tools.coordination_client get TASK_UUID --history
python -m tools.coordination_client get TASK_UUID --history --before-version 201 --limit 100
python -m tools.coordination_client get TASK_UUID --all-history
python -m tools.coordination_client --session-id coordinator-20261115 create --json task.json
python -m tools.coordination_client --session-id reviewer-20261115 action TASK_UUID review \
  --expected-version 5 --idempotency-key review-example-001 --json review.json
```

Use actual UUIDs and the current version from `get`. Example `task.json`:

```json
{
  "idempotency_key": "snapshot-example-001",
  "project": "cognitive_core",
  "title": "Record the approved worktree state",
  "acceptance": "Independent session checks branch, SHA, dirty status and artifact hash",
  "next_step": "Explicitly enqueue workspace_snapshot",
  "worktree": "/srv/cogcore-worktrees/example"
}
```

Example `review.json`, after actually checking the saved artifact and criteria:

```json
{
  "verdict": "approve",
  "detail": "Independent inspection confirmed the task's acceptance criteria",
  "evidence": [{"uri": "file:///var/lib/cogcore-coordination/artifacts/ACTUAL_FILE.json",
                "description": "Independent comparison with the approved worktree"}]
}
```

For `heartbeat`, `recover` and `submit`, include the current `attempt_id` using
`--attempt-id` or the JSON body. `recover` requires `reconciliation`; coordinator
`retry` also requires reconciliation of previous effects. All mutations require
session and idempotency key. Actions additionally require `expected_version`.
The same network retry must reuse the **exact body and key**. A changed body needs
a new key. The CLI emits JSON errors to stderr and nonzero exit status. API
redirects are refused to avoid forwarding credentials elsewhere. A read-only
`list --all` follows `next_cursor`; raw pages support status, cursor and limit.
History pages return `events`, `next_cursor` and `limit`. To inspect an older
page, pass its integer `next_cursor` as `--before-version` (exclusive), with a
page size of 1–200. `get --all-history` follows every older page and prints all
events in chronological version order, including old evidence. It also accepts
`--before-version` to start from an older point. Invalid or repeated cursors fail
explicitly instead of looping or silently dropping earlier evidence.

Start the worker first so DBOS initializes its technical schema and queue:

```sh
.venv-coordination/bin/python -m tools.coordination_worker start
```

Then, in another process with the same worker configuration:

```sh
.venv-coordination/bin/python -m tools.coordination_worker enqueue TASK_UUID
```

`enqueue` explicitly claims one queued Core task and records a workflow with ID
`coord:<task_id>:<attempt_id>`. It validates the worktree before claiming and
rereads Core after claim; an old cached HTTP claim receipt is not authority.
Restart reconciliation follows all `status=running` pages and considers only the
exact configured agent/session. It never polls or claims arbitrary queued tasks.
It also repairs a crash after claim but before enqueue by recreating the same
workflow ID for that same running attempt.

## Artifact, recovery and cancellation semantics

The artifact is `<task_id>.<attempt_id>.json`. It contains the input fingerprint,
task/attempt, resolved worktree, observation timestamp, branch, commit, porcelain
Git status and a content SHA-256. Publication fsyncs a temporary file then creates
an exclusive atomic hard link. A competing/recovered invocation validates and
uses the existing file; it never replaces it or creates a second final artifact.
The artifact filesystem must support hard links within this directory. A crash
can leave an inert `.coord-*.tmp` file; such files are not evidence and may be
removed later after confirming that no worker is active.

Before Git work, artifact publication and submission the step rereads Core and
checks the same worker/session/attempt, input fingerprint and cancellation state.
An expired lease can be recovered only for that same still-running attempt after
reconciliation. Before continuing after publication, the worker verifies the
existing artifact. A crash after Core accepted submission but before DBOS saved
the result is resolved by comparing current Core evidence with the exact file
hash; it does not re-submit or claim success from an old receipt.

The filesystem and Core do not share a transaction. Cancellation racing the last
check may leave an inert artifact for the old attempt. A fresh fence and the Core
version/attempt check prevent submission after cancellation; the unique attempt
filename prevents overwriting a later attempt. Allowlisted worktrees and the
artifact directory must not be writable by an untrusted process during a run.
This snapshot is a dated observation, not an atomic repository-wide freeze:
concurrent work can change Git state while it is being read.

Network/server failures are retried five times by the at-least-once DBOS step.
Exhausted or permanent failures try to mark the same Core attempt `blocked`, then
leave the workflow in `ERROR`. If that mutation is rejected or Core is unavailable,
the workflow still fails; it never returns successful completion. Inspect the
error, ledger history and old artifact before the coordinator blocks/retries.
Do not `resume` an errored workflow to bypass the Core retry protocol. A
coordinator retry creates a new attempt and therefore a new workflow ID.

## Repeatable verification

Unit/fencing/HTTP tests need pytest, independently of the API dependencies:

```sh
.venv-coordination/bin/python -m pip install pytest
.venv-coordination/bin/python -m pytest --noconftest tests/test_coordination_worker.py -q
```

The opt-in kill/restart cases need a **dedicated PostgreSQL database whose name
starts with `test_`**, provided through `COGCORE_COORD_TEST_DB_URL`. They use an
isolated loopback fake Core ledger, temporary Git worktree/artifacts, unique
application/session identities, and the real pinned DBOS process and database.
They leave diagnostic DBOS records in that test database and never delete a
database or reuse a production credential. They verify:

1. Process exit after Core claim but before DBOS enqueue; startup reconciliation
   restores the single claim/attempt and submits exactly once.
2. Process exit after fsynced publication but before a DBOS step checkpoint;
   the test expires the lease, restarts the same executor/session and checks a
   `recover` action, byte-identical single artifact and one submission to review.
3. The DBOS workflow/step records contain neither the isolated test API key nor
   a credential/client configuration in workflow inputs.

```sh
# Supply COGCORE_COORD_TEST_DB_URL securely in the test process environment.
.venv-coordination/bin/python -m pytest --noconftest tests/test_coordination_worker.py -q -k postgres
```

These tests establish DBOS process recovery, not the production Core integration.
Separately run the ledger/API tests against real PostgreSQL and repeat the worker
acceptance flow against a staged Core endpoint. Production installation,
credentials, unit activation and deployment remain separate authorized actions.

DBOS references checked against the installed 3.5.0 wheel: [configuration](https://github.com/dbos-inc/dbos-transact-py/blob/3.5.0/dbos/_dbos_config.py),
[queue registration](https://github.com/dbos-inc/dbos-transact-py/blob/3.5.0/dbos/_queue.py),
[client](https://github.com/dbos-inc/dbos-transact-py/blob/3.5.0/dbos/_client.py).
In this version `DBOS.listen_queues` runs before launch and
`DBOS.register_queue` after launch; `DBOSClient` never creates the schema.
