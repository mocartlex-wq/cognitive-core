"""Owner-scoped PostgreSQL task ledger, fenced attempts and independent review.

orchestrator_tasks is the only business queue. DBOS may keep execution history,
but cannot mark a business task verified_done. All mutations lock one task row,
check its version, and persist their receipt in the same transaction.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException

from app.models.coordination import TaskAction, TaskCreate

TERMINAL = {"verified_done", "failed", "cancelled"}


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fail(message: str, code: int = 409):
    raise HTTPException(status_code=code, detail=message)


def view(row, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    data = unpack(row["coord_data"])
    lease = row["coord_lease_until"]
    return {
        "task_id": str(row["task_id"]), "status": row["status"],
        "version": row["coord_version"], **data,
        "description": row["request_text"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["coord_updated_at"].isoformat(),
        "lease_until": lease.isoformat() if lease else None,
        "lease_expired": bool(lease and lease <= now and row["status"] == "running"),
    }


def transition(task: dict, body: TaskAction, actor: dict, *, now: datetime,
               dependencies_ready: bool = True, account_user: bool = False) -> dict:
    """Pure state transition; producer claims are never independent acceptance."""
    if task["version"] != body.expected_version:
        fail("stale_version: перечитайте задачу перед изменением")
    item = copy.deepcopy(task)
    coordinator = actor == item["coordinator"] or account_user
    worker = actor == item.get("worker")
    same_attempt = str(body.attempt_id) == item.get("attempt_id") and body.attempt_id is not None
    active = worker and same_attempt and item["status"] == "running"
    expired = bool(item.get("lease_until") and datetime.fromisoformat(item["lease_until"]) <= now)
    action = body.action
    if action == "claim":
        if item["status"] != "queued" or not dependencies_ready:
            fail("not_claimable: задача занята либо зависимости не приняты")
        for field, actor_field in (("assigned_agent_id", "agent_id"), ("assigned_session_id", "session_id")):
            if item.get(field) and item[field] != actor[actor_field]:
                fail("assigned_to_another_executor", 403)
        item.update(status="running", worker=actor, attempt_id=str(uuid4()),
                    attempt_no=item.get("attempt_no", 0) + 1, blocker="", review=None)
        item["lease_until"] = (now + timedelta(seconds=body.lease_seconds)).isoformat()
    elif action in {"heartbeat", "recover"}:
        if not active or (expired and action != "recover"):
            fail("stale_attempt_or_lease: другой сеанс не может продлить попытку")
        if action == "recover" and not body.reconciliation.strip():
            fail("recovery_requires_reconciliation", 422)
        item["lease_until"] = (now + timedelta(seconds=body.lease_seconds)).isoformat()
    elif action == "submit":
        if not active or expired:
            fail("stale_attempt_or_lease")
        if not body.detail.strip() or not body.evidence:
            fail("result_and_evidence_required", 422)
        item.update(status="review", result=body.detail,
                    evidence=[x.model_dump(exclude_none=True) for x in body.evidence],
                    next_step=body.next_step or "Независимая проверка результата", lease_until=None)
    elif action == "review":
        if item["status"] != "review" or not body.verdict:
            fail("not_awaiting_review")
        if worker:
            fail("independent_session_required: исполнитель не принимает свою работу", 403)
        if not body.detail.strip() or not body.evidence:
            fail("review_evidence_required", 422)
        item["review"] = {"actor": actor, "verdict": body.verdict, "detail": body.detail,
                          "evidence": [x.model_dump(exclude_none=True) for x in body.evidence],
                          "observed_at": now.isoformat()}
        if body.verdict == "approve":
            item.update(status="verified_done", next_step="Завершено после проверки", blocker="")
        else:
            item.update(status="blocked", blocker=body.detail,
                        next_step=body.next_step or "Исправить замечания и повторить проверку")
    elif action in {"block", "fail", "cancel"}:
        if item["status"] in TERMINAL:
            fail("terminal_task")
        if action == "cancel":
            allowed = coordinator
        else:
            allowed = coordinator or (active and not expired)
        if not allowed:
            fail("actor_does_not_own_attempt", 403)
        if not body.detail.strip():
            fail("reason_required", 422)
        item.update(status={"block": "blocked", "fail": "failed", "cancel": "cancelled"}[action],
                    blocker=body.detail, next_step=body.next_step or body.detail, lease_until=None)
    elif action == "retry":
        if not coordinator:
            fail("coordinator_required", 403)
        if item["status"] not in {"blocked", "failed"}:
            fail("not_retryable")
        if not body.reconciliation.strip():
            fail("retry_requires_reconciliation", 422)
        item.update(status="queued", worker=None, attempt_id=None, lease_until=None,
                    blocker="", result=None, evidence=[], review=None,
                    next_step=body.next_step or "Повторить после сверки прежнего результата")
    elif action == "assign":
        if not coordinator:
            fail("coordinator_required", 403)
        if item["status"] not in {"queued", "blocked"}:
            fail("cannot_reassign_active_attempt")
        item.update(assigned_agent_id=body.assigned_agent_id, assigned_session_id=body.assigned_session_id)
    if body.next_step and action not in {"review", "submit", "retry"}:
        item["next_step"] = body.next_step
    item.update(version=item["version"] + 1, updated_at=now.isoformat(), lease_expired=False)
    return item


async def create_task(pool, owner: str, actor: dict, body: TaskCreate) -> dict:
    task_id = uuid5(NAMESPACE_URL, f"cogcore-task:{owner}:{body.idempotency_key}")
    digest = fingerprint({"actor": actor, "body": body.model_dump(mode="json")})
    data = body.model_dump(mode="json", exclude={"idempotency_key", "session_id", "description"})
    data.update(schema_version=1, coordinator=actor, worker=None, attempt_id=None,
                attempt_no=0, result=None, evidence=[], review=None, blocker="", create_hash=digest)
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchrow("SELECT * FROM orchestrator_tasks WHERE task_id=$1 AND owner_user_id=$2", task_id, UUID(owner))
        if existing:
            if unpack(existing["coord_data"]).get("create_hash") != digest:
                fail("idempotency_key_reused_with_different_input")
            return view(existing)
        if body.depends_on:
            count = await conn.fetchval("SELECT count(*) FROM orchestrator_tasks WHERE task_id=ANY($1::uuid[]) AND owner_user_id=$2 AND coord_data IS NOT NULL",
                                        list(set(body.depends_on)), UUID(owner))
            if count != len(set(body.depends_on)):
                fail("dependency_not_found", 404)
        await conn.execute("""INSERT INTO orchestrator_tasks
            (task_id,user_id,owner_user_id,request_text,context,session_id,coord_data)
            VALUES($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb) ON CONFLICT(task_id) DO NOTHING""",
            task_id, owner, UUID(owner), body.description or body.title,
            canonical({"coordination_v1": True}), actor["session_id"], canonical(data))
        row = await conn.fetchrow("SELECT * FROM orchestrator_tasks WHERE task_id=$1 AND owner_user_id=$2", task_id, UUID(owner))
        if unpack(row["coord_data"]).get("create_hash") != digest:
            fail("idempotency_key_reused_with_different_input")
        return view(row)


async def list_tasks(pool, owner: str, project: str | None, limit: int, status: str | None = None, cursor: str | None = None) -> dict:
    timestamp, last_id = None, None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            timestamp, last_id = datetime.fromisoformat(decoded[0]), UUID(decoded[1])
            if timestamp.tzinfo is None:
                raise ValueError("timezone required")
        except (ValueError, TypeError, IndexError, KeyError, UnicodeError):
            fail("invalid_cursor", 422)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT * FROM orchestrator_tasks WHERE owner_user_id=$1
            AND coord_data IS NOT NULL AND ($2::text IS NULL OR coord_data->>'project'=$2)
            AND ($3::text IS NULL OR status=$3)
            AND ($4::timestamptz IS NULL OR (coord_updated_at,task_id)<($4,$5::uuid))
            ORDER BY coord_updated_at DESC,task_id DESC LIMIT $6""", UUID(owner), project, status, timestamp, last_id, limit + 1)
    more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if more:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last["coord_updated_at"].isoformat(), str(last["task_id"])]).encode()).decode()
    return {"tasks": [view(r) for r in page], "next_cursor": next_cursor,
            "source": "cognitive_core.orchestrator_tasks", "limit": limit}


async def read_task(pool, owner: str, task_id: UUID) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orchestrator_tasks WHERE task_id=$1 AND owner_user_id=$2 AND coord_data IS NOT NULL", task_id, UUID(owner))
    if not row:
        fail("task_not_found", 404)
    return view(row)


async def act(pool, owner: str, task_id: UUID, actor: dict, body: TaskAction, *, account_user: bool = False) -> dict:
    digest = fingerprint({"actor": actor, "body": body.model_dump(mode="json")})
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM orchestrator_tasks WHERE task_id=$1 AND owner_user_id=$2 AND coord_data IS NOT NULL FOR UPDATE", task_id, UUID(owner))
        if not row:
            fail("task_not_found", 404)
        receipt = await conn.fetchrow("SELECT request_hash,result FROM coord_task_operations WHERE task_id=$1 AND operation_key=$2", task_id, body.idempotency_key)
        if receipt:
            if receipt["request_hash"] != digest:
                fail("idempotency_key_reused_with_different_input")
            return unpack(receipt["result"])
        # Evaluate the lease after acquiring the lock, not at transaction start.
        # A request waiting behind a slow transaction must not resurrect a lease.
        db_now = await conn.fetchval("SELECT clock_timestamp()")
        before = view(row, db_now)
        deps = [UUID(x) for x in before.get("depends_on", [])]
        ready = True
        if body.action == "claim" and deps:
            count = await conn.fetchval("SELECT count(*) FROM orchestrator_tasks WHERE task_id=ANY($1::uuid[]) AND owner_user_id=$2 AND status='verified_done' AND coord_data IS NOT NULL", deps, UUID(owner))
            ready = count == len(set(deps))
        after = transition(before, body, actor, now=db_now, dependencies_ready=ready, account_user=account_user)
        data = {k: v for k, v in after.items() if k not in {"task_id", "status", "version", "description", "created_at", "updated_at", "lease_until", "lease_expired"}}
        lease = datetime.fromisoformat(after["lease_until"]) if after["lease_until"] else None
        await conn.execute("""UPDATE orchestrator_tasks SET status=$2,coord_data=$3::jsonb,
            coord_version=$4,coord_updated_at=$5::timestamptz,coord_lease_until=$6,
            completed_at=CASE WHEN $2 IN ('verified_done','failed','cancelled') THEN $5::timestamptz ELSE NULL END
            WHERE task_id=$1""", task_id, after["status"], canonical(data), after["version"], db_now, lease)
        await conn.execute("""INSERT INTO coord_task_operations(task_id,operation_key,request_hash,actor,action,result,request_data)
            VALUES($1,$2,$3,$4::jsonb,$5,$6::jsonb,$7::jsonb)""", task_id, body.idempotency_key, digest, canonical(actor), body.action,
            canonical(after), canonical(body.model_dump(mode="json", exclude_none=True, exclude={"idempotency_key"})))
        return after


async def history(pool, owner: str, task_id: UUID, limit: int = 200, before_version: int | None = None) -> dict[str, Any]:
    await read_task(pool, owner, task_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT actor,action,result,request_data,created_at FROM coord_task_operations
            WHERE task_id=$1 AND ($2::bigint IS NULL OR (result->>'version')::bigint<$2)
            ORDER BY (result->>'version')::bigint DESC LIMIT $3""", task_id, before_version, limit + 1)
    more, page = len(rows) > limit, rows[:limit]
    events = []
    for row in reversed(page):
        result, request = unpack(row["result"]), unpack(row["request_data"])
        events.append({"actor": unpack(row["actor"]), "action": row["action"],
            "status": result["status"], "version": result["version"], "at": row["created_at"].isoformat(),
            **{key: request.get(key) for key in ("detail", "reconciliation", "next_step", "evidence")},
            **{key: result.get(key) for key in ("attempt_id", "worker", "result", "review")}})
    return {"events": events, "next_cursor": events[0]["version"] if more else None, "limit": limit}
