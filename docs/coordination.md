# Shared task ownership and acceptance

`orchestrator_tasks` is the single business queue. Rows created by `/coord/tasks`
have `context.coordination_v1=true`; the legacy consultation service ignores
them. DBOS is a separate execution log, not a competing source of task status.
The existing shared-memory service remains the source of context and checkpoints.

The browser panel is `/coordination`, served by Core, using the owner's normal
account cookie. It supports creating tasks, reading current state/history and
independent acceptance. Agent clients use the same owner-scoped API or native MCP
tools `coord_task_create/list/get/history/action`. Legacy consultation `completed`
only means a substantive answer arrived; coordinated work requires `verified_done`.

## Task protocol

1. A coordinator creates a task with project, acceptance criteria, next step,
   stable session ID and idempotency key. Optional dependencies must be accepted
   before a claim, and an assignment can limit the eligible executor/session.
2. An executor claims `queued`, receiving a version, attempt UUID and lease.
   Mutations require the expected version and an idempotency key. A repeated
   identical request returns its original receipt. Changed input cannot reuse it.
3. A live worker can heartbeat, or recover its expired **same attempt** after
   recording reconciliation. Another session cannot take over that attempt.
   The coordinator can block it, reconcile effects and explicitly retry, creating
   a new attempt. Cancellation fences late results.
4. Submission with a result and evidence enters `review`. A different session
   checks the artifact and records its own evidence. Approval produces
   `verified_done`; rejection produces `blocked`. Declaring success in a message
   or a DBOS status does not accept the business task.
5. History preserves result/evidence/review and recovery rationale for earlier
   attempts. Follow `next_cursor` as `before_version`; task listing has its own
   opaque keyset cursor. The panel labels counters as the loaded selection.

Agent identity comes from authentication. Session identity is client-supplied:
this prevents accidental self-acceptance, but is not a cryptographic proof that
two sessions are operated by different people or models. Independent verification
still requires actually reading the artifact in another execution context.

External effects cannot generally be made exactly-once by an HTTP task ledger.
The first worker is deliberately limited to read-only Git inspection and an
idempotent local JSON publication. It never claims arbitrary queued tasks or
executes the text of a task. See [worker configuration](coordination-worker.md).
Chat messages are not automatically imported as tasks, and the previous local
`tasks.json` dashboard remains an explicitly labelled registry until migrated.

## Release and recovery

1. Follow the repository rule in `CLAUDE.md`: explicit owner approval for the
   concrete PR before merging. Check current server HEAD and ownership, current
   legacy consultations, a restorable database backup, and service health.
2. Allow current legacy consultations to finish before the host-daemon restart.
   `conditional_reload.sh` now installs and verifies the changed
   `/usr/local/lib/cognitive-orchestrator.py` before the API rebuild. An absent
   host service is skipped; syntax or restart failure stops rollout.
3. Alembic `0027` adds columns and an operation ledger. Existing consultation rows
   are retained. Check migration head, anonymous denial, owner isolation and a
   small create/claim/submit/review flow after the API becomes healthy.
4. Open `/coordination` with the normal account session. Verify history and
   evidence, including a rejection. Only then connect each client to these MCP
   tools and register its explicit project/role/session. Do not import or claim
   tasks already owned by another chat without reconciling ownership.
5. Provision the worker's separate DBOS database/venv/account and existing
   owner-scoped credential outside Git. The unit is a template and is not
   automatically enabled by this release. Start with one allowlisted worktree,
   one explicitly enqueued snapshot and independent acceptance.

Rollback keeps the additive schema and all task/operation rows. Disable the new
worker and new-task creation, reconcile running attempts, and use the deployment
code rollback with `COGNITIVE_MIGRATE=0`. Retain the updated legacy marker fence
while coordinated rows exist. If rolling the host daemon back too, restore it
only after confirming its endpoints cannot access coordinated rows. Never use
`alembic downgrade` on a populated coordination ledger: the migration deliberately
refuses it under an exclusive table lock. A schema downgrade is only supported
on an empty coordination ledger.

Backing up the local Codex workspace is not a PostgreSQL/DBOS backup. Back up
both databases, artifact files and the stable worker identity/configuration using
the owner's private server backup process. Restore them together; changing a
worker session or workflow version while executions remain can strand recovery.

## Verification

- Pure transition tests cover version/attempt fences, expired leases, cancellation,
  dependencies, assignments, proof validation and the independent review gate.
- PostgreSQL tests use a random schema in explicit `COGCORE_TEST_DB_URL`, checking
  concurrent claims/create, transaction rollback, tenant boundaries, pagination,
  retained historical evidence and a concurrent downgrade/create guard.
- Real HTTP/native-MCP tests use synthetic owners in the explicit test database,
  checking cookie CSRF, anonymous/foreign-owner denial and the complete review flow.
- Worker crash tests use an opt-in separate `test_*` DBOS database. Run the full
  suite on a fresh PostgreSQL/Redis stack as CI does: legacy claim-flow fixtures
  use fixed agent names and are not repeatable against a previously used database.

The initial acceptance used separate Core/PostgreSQL/Redis/DBOS infrastructure,
an actual process exit after publication, an expired lease simulated in that
isolated DB, restart recovery and independent Git/hash verification. The same
attempt produced one artifact and one submission, then the real browser accepted
it from another session. This does not establish long-running service reliability,
Claude compatibility, arbitrary write-side effects or automatic control of all chats.
