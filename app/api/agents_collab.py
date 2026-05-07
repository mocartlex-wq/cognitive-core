"""Multi-agent collaboration endpoints (v0.5.0-prod #3 + start of v0.5.5).

Provides:
- POST /agents/register   — register new agent, get back fresh per-agent API key
- POST /agents/heartbeat  — refresh presence + current_task
- GET  /agents/online     — list agents with heartbeat in last 60 seconds
- POST /agents/message    — direct message agent->agent (stored in L1 events)
- GET  /agents/inbox      — unread/recent direct messages for me
- POST /agents/keys/revoke — revoke specific api_key

Identity model: каждый агент сам выбирает читаемый agent_id (например
'cognitive-core-laptop-mocartlex' или 'ai-crm-deploy-anthropic'). Ключ
выдаётся при register и используется в X-API-Key header.

Direct messages stored as L1 events with domain='agent_inbox' so they:
1) automatically participate in L1->L2->L3 consolidation (long-term memory)
2) appear in L5 audit log
3) survive Redis restart (Postgres-durable)

This is the durable channel. Fast-memory L0 (Redis blackboard, presence
TTL, scratchpad, pub/sub, locks) lands in v0.5.5. Polling /inbox every
5-10 sec is fine for MVP.
"""
import json
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.db.postgres import get_pool
from app.security.auth import verify_api_key

router = APIRouter(prefix="/agents", tags=["multi-agent"])


# ─── Models ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    project: str | None = Field(None, max_length=64)
    machine: str | None = Field(None, max_length=128)
    capabilities: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=512)


class RegisterResponse(BaseModel):
    agent_id: str
    api_key: str
    note: str = "Save this api_key — it is shown only once. Use it in X-API-Key header."


class HeartbeatRequest(BaseModel):
    current_task: str | None = Field(None, max_length=512)


class MessageRequest(BaseModel):
    to: str = Field(..., min_length=3, max_length=64)
    text: str = Field(..., min_length=1, max_length=8192)
    context: dict = Field(default_factory=dict)


class MessageOut(BaseModel):
    id: str
    from_agent: str
    to: str
    text: str
    context: dict
    sent_at: str


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _new_api_key() -> str:
    """64-hex-char random key. Cryptographically secure."""
    return secrets.token_hex(32)


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/register", response_model=RegisterResponse)
async def register_agent(req: RegisterRequest):
    """Register a new agent and return a fresh per-agent API key.

    No auth required for registration (bootstrap scenario). Operator can
    audit via agent_keys.created_at and revoke abusers via /keys/revoke.
    """
    pool = await get_pool()
    api_key = _new_api_key()
    async with pool.acquire() as conn:
        # Upsert agent_states row
        await conn.execute(
            """
            INSERT INTO agent_states (agent_id, project, machine, capabilities, last_heartbeat_at)
            VALUES ($1, $2, $3, $4::jsonb, NOW())
            ON CONFLICT (agent_id) DO UPDATE SET
                project = EXCLUDED.project,
                machine = EXCLUDED.machine,
                capabilities = EXCLUDED.capabilities,
                last_heartbeat_at = NOW(),
                updated_at = NOW()
            """,
            req.agent_id, req.project, req.machine,
            json.dumps(req.capabilities, ensure_ascii=False),
        )
        # Issue new key
        await conn.execute(
            """
            INSERT INTO agent_keys (api_key, agent_id, description)
            VALUES ($1, $2, $3)
            """,
            api_key, req.agent_id, req.description,
        )
    return RegisterResponse(agent_id=req.agent_id, api_key=api_key)


@router.post("/heartbeat")
async def heartbeat(req: HeartbeatRequest, agent_id: str = Depends(verify_api_key)):
    """Update last_heartbeat_at + current_task. Call every 30s for presence."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_states
            SET last_heartbeat_at = NOW(),
                current_task = COALESCE($2, current_task),
                updated_at = NOW()
            WHERE agent_id = $1
            """,
            agent_id, req.current_task,
        )
    return {"ok": True, "agent_id": agent_id, "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/online")
async def online(
    project: str | None = Query(None),
    within_seconds: int = Query(120, ge=10, le=3600),
    _: str = Depends(verify_api_key),
):
    """List agents with heartbeat in last `within_seconds` (default 120)."""
    pool = await get_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    async with pool.acquire() as conn:
        if project:
            rows = await conn.fetch(
                """
                SELECT agent_id, project, machine, current_task, capabilities, last_heartbeat_at
                FROM agent_states
                WHERE last_heartbeat_at >= $1 AND project = $2
                ORDER BY last_heartbeat_at DESC
                """,
                cutoff, project,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT agent_id, project, machine, current_task, capabilities, last_heartbeat_at
                FROM agent_states
                WHERE last_heartbeat_at >= $1
                ORDER BY last_heartbeat_at DESC
                """,
                cutoff,
            )
    return {
        "count": len(rows),
        "agents": [
            {
                "agent_id": r["agent_id"],
                "project": r["project"],
                "machine": r["machine"],
                "current_task": r["current_task"],
                "capabilities": json.loads(r["capabilities"]) if isinstance(r["capabilities"], str) else r["capabilities"],
                "last_heartbeat": r["last_heartbeat_at"].isoformat(),
                "seconds_ago": int((datetime.now(timezone.utc) - r["last_heartbeat_at"]).total_seconds()),
            }
            for r in rows
        ],
    }


@router.post("/message")
async def send_message(req: MessageRequest, agent_id: str = Depends(verify_api_key)):
    """Send direct message to another agent. Stored as L1 event for durability."""
    pool = await get_pool()
    payload = {
        "from": agent_id,
        "to": req.to,
        "text": req.text,
        "context": req.context,
    }
    async with pool.acquire() as conn:
        # Verify recipient exists (not strict — allow message-to-future-agent)
        recipient_exists = await conn.fetchval(
            "SELECT 1 FROM agent_states WHERE agent_id = $1", req.to
        )
        # Insert as L1 event in agent_inbox domain
        event_id = await conn.fetchval(
            """
            INSERT INTO l1_raw_events (agent_id, domain, event_type, payload)
            VALUES ($1, $2, 'agent_message', $3::jsonb)
            RETURNING event_id
            """,
            agent_id, f"agent_inbox", json.dumps(payload, ensure_ascii=False),
        )
    return {
        "ok": True,
        "id": str(event_id),
        "to": req.to,
        "recipient_known": bool(recipient_exists),
    }


@router.get("/inbox")
async def inbox(
    since_minutes: int = Query(60, ge=1, le=10080),
    limit: int = Query(50, ge=1, le=500),
    agent_id: str = Depends(verify_api_key),
):
    """Get direct messages addressed to me, newest first."""
    pool = await get_pool()
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_id, agent_id AS from_agent, payload, timestamp
            FROM l1_raw_events
            WHERE domain = 'agent_inbox'
              AND timestamp >= $1
              AND payload->>'to' = $2
            ORDER BY timestamp DESC
            LIMIT $3
            """,
            since, agent_id, limit,
        )
    messages = []
    for r in rows:
        p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        messages.append({
            "id": str(r["event_id"]),
            "from": p.get("from", r["from_agent"]),
            "text": p.get("text", ""),
            "context": p.get("context", {}),
            "sent_at": r["timestamp"].isoformat(),
        })
    return {"count": len(messages), "messages": messages}


class RevokeRequest(BaseModel):
    api_key: str


@router.post("/keys/revoke")
async def revoke_key(req: RevokeRequest, agent_id: str = Depends(verify_api_key)):
    """Revoke an api_key. Owner of the key OR an admin token can call."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check ownership: only allow if revoking own key OR caller has admin tag.
        # MVP: only own keys.
        owner = await conn.fetchval(
            "SELECT agent_id FROM agent_keys WHERE api_key = $1 AND revoked_at IS NULL",
            req.api_key,
        )
        if owner is None:
            raise HTTPException(status_code=404, detail="Key not found or already revoked")
        if owner != agent_id:
            raise HTTPException(status_code=403, detail="Can only revoke own keys")
        await conn.execute(
            "UPDATE agent_keys SET revoked_at = NOW() WHERE api_key = $1",
            req.api_key,
        )
    return {"ok": True, "revoked": req.api_key[:8] + "…"}
