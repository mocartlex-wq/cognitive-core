"""Endpoints для agent operating rules (Phase 6).

Tenant CRUD:
  GET    /user/rules                       — мои user rules + count платформенных (без body, IP)
  POST   /user/rules                       — создать user rule
  PATCH  /user/rules/{id}                  — изменить свой rule
  DELETE /user/rules/{id}                  — удалить свой rule

Proposals (для предложения НОВЫХ платформенных правил):
  GET    /user/rules/proposals             — мои + публичные pending
  POST   /user/rules/proposals             — предложить новое правило
  POST   /user/rules/proposals/{id}/vote   — ±1 голос (один tenant — один голос)

Admin (is_admin=TRUE only):
  GET    /admin/rule-proposals             — список pending/reviewing
  POST   /admin/rule-proposals/{id}/approve — promote в platform rule
  POST   /admin/rule-proposals/{id}/reject  — пометить rejected с notes

Все требуют session через require_user.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.postgres import get_pool
from app.security.middleware import require_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rules"])


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("id", "owner_user_id", "promoted_from", "override_of",
              "reviewed_by", "promoted_rule_id", "ds_duplicate_of"):
        v = d.get(k)
        if v is not None:
            d[k] = str(v)
    for k in ("created_at", "updated_at", "reviewed_at"):
        d[k] = _iso(d.get(k))
    return d


async def _require_admin(request: Request):
    user = await require_user(request)
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="admin required")
    return user


VALID_SCOPES = {"pre-answer", "post-task", "per-task", "mid-task", "general"}


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────

class CreateRuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(..., min_length=3, max_length=128, pattern=r"^[\w.\-]+$")
    severity: str = Field("user", pattern=r"^(user|recommended)$")  # tenant не может core
    scope: str = Field(..., max_length=32)
    body: str = Field(..., min_length=5, max_length=2000)
    lang: str = Field("ru", max_length=8)
    position: int = Field(100, ge=0, le=10000)

    @field_validator("scope")
    @classmethod
    def _scope_valid(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}")
        return v


class PatchRuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str | None = Field(None, min_length=5, max_length=2000)
    scope: str | None = Field(None, max_length=32)
    position: int | None = Field(None, ge=0, le=10000)
    active: bool | None = None

    @field_validator("scope")
    @classmethod
    def _scope_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}")
        return v


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposed_body: str = Field(..., min_length=10, max_length=2000)
    proposed_scope: str = Field(..., max_length=32)
    rationale: str | None = Field(None, max_length=1000)

    @field_validator("proposed_scope")
    @classmethod
    def _scope_valid(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}")
        return v


class VoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vote: int = Field(..., ge=-1, le=1)


class AdminReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: str | None = Field(None, pattern=r"^(core|recommended)$")
    review_notes: str | None = Field(None, max_length=2000)


# ─────────────────────────────────────────────────────────────────────────
# Tenant endpoints — /user/rules
# ─────────────────────────────────────────────────────────────────────────

@router.get("/user/rules")
async def list_user_rules(request: Request) -> dict[str, Any]:
    """Возвращает:
      - own user rules (body показывается)
      - platform_count (платформенные скрыты — это IP)
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        own = await conn.fetch(
            """
            SELECT id, owner_user_id, rule_id, severity, scope, lang, position, body,
                   active, source, override_of, created_at, updated_at
            FROM agent_rules
            WHERE owner_user_id = $1::uuid
            ORDER BY position, created_at
            """,
            user.user_id,
        )
        platform_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_rules WHERE owner_user_id IS NULL AND active = TRUE"
        )
    return {
        "platform_count": int(platform_count or 0),
        "platform_notice": (
            f"Платформа применяет {platform_count} базовых правил для качества и "
            "безопасности работы агентов. Эти правила формируют baseline поведения всех "
            "агентов на платформе."
        ),
        "items": [_row_to_dict(r) for r in own],
    }


@router.post("/user/rules", status_code=201)
async def create_user_rule(body: CreateRuleBody, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check duplicate rule_id для этого owner'a
        dup = await conn.fetchval(
            "SELECT 1 FROM agent_rules WHERE owner_user_id = $1::uuid AND rule_id = $2",
            user.user_id, body.rule_id,
        )
        if dup:
            raise HTTPException(status_code=409, detail=f"Rule with id «{body.rule_id}» already exists")

        row = await conn.fetchrow(
            """
            INSERT INTO agent_rules
              (owner_user_id, rule_id, severity, scope, lang, position, body, source)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, 'user')
            RETURNING id, owner_user_id, rule_id, severity, scope, lang, position, body,
                      active, source, override_of, created_at, updated_at
            """,
            user.user_id, body.rule_id, body.severity, body.scope,
            body.lang, body.position, body.body,
        )
    return _row_to_dict(row)


@router.patch("/user/rules/{rule_uuid}")
async def patch_user_rule(rule_uuid: str, body: PatchRuleBody, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify ownership — нельзя править платформенные
        owner_check = await conn.fetchval(
            "SELECT owner_user_id FROM agent_rules WHERE id = $1::uuid",
            rule_uuid,
        )
        if owner_check is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        if str(owner_check) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Not your rule (platform rules immutable)")

        sets: list[str] = []
        args: list[Any] = []
        idx = 1
        for k in ("body", "scope", "position", "active"):
            v = getattr(body, k, None)
            if v is not None:
                sets.append(f"{k} = ${idx}")
                args.append(v)
                idx += 1
        if not sets:
            raise HTTPException(status_code=400, detail="Nothing to update")
        sets.append("updated_at = NOW()")
        args.append(rule_uuid)
        row = await conn.fetchrow(
            f"""
            UPDATE agent_rules SET {', '.join(sets)}
            WHERE id = ${idx}::uuid
            RETURNING id, owner_user_id, rule_id, severity, scope, lang, position, body,
                      active, source, override_of, created_at, updated_at
            """,
            *args,
        )
    return _row_to_dict(row)


@router.delete("/user/rules/{rule_uuid}")
async def delete_user_rule(rule_uuid: str, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner_check = await conn.fetchval(
            "SELECT owner_user_id FROM agent_rules WHERE id = $1::uuid",
            rule_uuid,
        )
        if owner_check is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        if str(owner_check) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Not your rule")
        await conn.execute("DELETE FROM agent_rules WHERE id = $1::uuid", rule_uuid)
    return {"ok": True, "deleted": rule_uuid}


# ─────────────────────────────────────────────────────────────────────────
# Proposals — /user/rules/proposals
# ─────────────────────────────────────────────────────────────────────────

@router.get("/user/rules/proposals")
async def list_proposals(request: Request) -> dict[str, Any]:
    """Returns: own proposals (any status) + recent public pending для voting."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        own = await conn.fetch(
            """
            SELECT id, owner_user_id, proposed_body, proposed_scope, rationale,
                   status, votes_up, votes_down, vote_threshold,
                   ds_analysis, ds_suggested_severity, ds_duplicate_of,
                   reviewed_by, reviewed_at, review_notes, promoted_rule_id, created_at
            FROM rule_proposals
            WHERE owner_user_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 50
            """,
            user.user_id,
        )
        public = await conn.fetch(
            """
            SELECT id, proposed_body, proposed_scope, rationale,
                   status, votes_up, votes_down, vote_threshold, created_at
            FROM rule_proposals
            WHERE status = 'pending' AND owner_user_id != $1::uuid
            ORDER BY votes_up DESC, created_at DESC
            LIMIT 20
            """,
            user.user_id,
        )
        # Mark which public ones owner already voted on
        voted = await conn.fetch(
            """
            SELECT proposal_id, vote FROM rule_proposal_votes
            WHERE owner_user_id = $1::uuid
            """,
            user.user_id,
        )
    voted_map = {str(r["proposal_id"]): r["vote"] for r in voted}
    pub_items = []
    for r in public:
        d = _row_to_dict(r)
        d["my_vote"] = voted_map.get(d["id"])
        pub_items.append(d)
    return {
        "own": [_row_to_dict(r) for r in own],
        "public": pub_items,
    }


@router.post("/user/rules/proposals", status_code=201)
async def create_proposal(body: ProposalBody, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO rule_proposals
              (owner_user_id, proposed_body, proposed_scope, rationale, status)
            VALUES ($1::uuid, $2, $3, $4, 'pending')
            RETURNING id, owner_user_id, proposed_body, proposed_scope, rationale,
                      status, votes_up, votes_down, vote_threshold, created_at
            """,
            user.user_id, body.proposed_body, body.proposed_scope, body.rationale,
        )
    return _row_to_dict(row)


@router.post("/user/rules/proposals/{proposal_id}/vote")
async def vote_proposal(proposal_id: str, body: VoteBody, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify proposal exists
        proposal = await conn.fetchrow(
            "SELECT id, owner_user_id, status FROM rule_proposals WHERE id = $1::uuid",
            proposal_id,
        )
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if proposal["status"] not in ("pending", "reviewing"):
            raise HTTPException(status_code=400, detail=f"Cannot vote on status={proposal['status']}")
        # Can't vote on own proposal
        if str(proposal["owner_user_id"]) == str(user.user_id):
            raise HTTPException(status_code=403, detail="Cannot vote on your own proposal")
        # Upsert vote
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT vote FROM rule_proposal_votes WHERE proposal_id=$1::uuid AND owner_user_id=$2::uuid",
                proposal_id, user.user_id,
            )
            if existing is not None:
                # Revert old, apply new
                if existing == 1:
                    await conn.execute("UPDATE rule_proposals SET votes_up = votes_up - 1 WHERE id=$1::uuid", proposal_id)
                else:
                    await conn.execute("UPDATE rule_proposals SET votes_down = votes_down - 1 WHERE id=$1::uuid", proposal_id)
            await conn.execute(
                """
                INSERT INTO rule_proposal_votes (proposal_id, owner_user_id, vote)
                VALUES ($1::uuid, $2::uuid, $3)
                ON CONFLICT (proposal_id, owner_user_id) DO UPDATE SET vote = EXCLUDED.vote
                """,
                proposal_id, user.user_id, body.vote,
            )
            if body.vote == 1:
                await conn.execute("UPDATE rule_proposals SET votes_up = votes_up + 1 WHERE id=$1::uuid", proposal_id)
            else:
                await conn.execute("UPDATE rule_proposals SET votes_down = votes_down + 1 WHERE id=$1::uuid", proposal_id)
            updated = await conn.fetchrow(
                "SELECT votes_up, votes_down, vote_threshold FROM rule_proposals WHERE id=$1::uuid",
                proposal_id,
            )
    return {"ok": True, "votes_up": updated["votes_up"], "votes_down": updated["votes_down"], "vote_threshold": updated["vote_threshold"]}


# ─────────────────────────────────────────────────────────────────────────
# Admin endpoints — /admin/rule-proposals
# ─────────────────────────────────────────────────────────────────────────

@router.get("/admin/rule-proposals")
async def admin_list_proposals(request: Request, status: str = "pending") -> dict[str, Any]:
    await _require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rp.id, rp.owner_user_id, a.email AS owner_email,
                   rp.proposed_body, rp.proposed_scope, rp.rationale,
                   rp.status, rp.votes_up, rp.votes_down, rp.vote_threshold,
                   rp.ds_analysis, rp.ds_suggested_severity, rp.ds_duplicate_of,
                   rp.reviewed_by, rp.reviewed_at, rp.review_notes,
                   rp.promoted_rule_id, rp.created_at
            FROM rule_proposals rp
            LEFT JOIN accounts a ON a.user_id = rp.owner_user_id
            WHERE rp.status = $1
            ORDER BY rp.votes_up DESC, rp.created_at DESC
            """,
            status,
        )
    return {"items": [_row_to_dict(r) for r in rows]}


@router.post("/admin/rule-proposals/{proposal_id}/approve")
async def admin_approve_proposal(
    proposal_id: str, body: AdminReviewBody, request: Request,
) -> dict[str, Any]:
    """Approve proposal → create new platform rule with chosen severity."""
    admin = await _require_admin(request)
    severity = body.severity or "recommended"
    pool = await get_pool()
    async with pool.acquire() as conn:
        proposal = await conn.fetchrow(
            "SELECT * FROM rule_proposals WHERE id = $1::uuid",
            proposal_id,
        )
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if proposal["status"] not in ("pending", "reviewing"):
            raise HTTPException(status_code=400, detail=f"Already {proposal['status']}")
        # Auto-generate rule_id from proposal id (or admin can provide)
        new_rule_id = f"rule-promoted-{str(proposal['id'])[:8]}"
        async with conn.transaction():
            new_rule = await conn.fetchrow(
                """
                INSERT INTO agent_rules
                  (owner_user_id, rule_id, severity, scope, lang, position, body,
                   source, promoted_from)
                VALUES (NULL, $1, $2, $3, 'ru', 1000, $4, 'promoted_from_user', NULL)
                RETURNING id
                """,
                new_rule_id, severity, proposal["proposed_scope"], proposal["proposed_body"],
            )
            await conn.execute(
                """
                UPDATE rule_proposals
                SET status='approved', reviewed_by=$1::uuid, reviewed_at=NOW(),
                    review_notes=$2, promoted_rule_id=$3::uuid
                WHERE id = $4::uuid
                """,
                admin.user_id, body.review_notes, new_rule["id"], proposal_id,
            )
    return {"ok": True, "promoted_rule_id": str(new_rule["id"]), "severity": severity}


@router.post("/admin/rule-proposals/{proposal_id}/reject")
async def admin_reject_proposal(
    proposal_id: str, body: AdminReviewBody, request: Request,
) -> dict[str, Any]:
    admin = await _require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE rule_proposals
            SET status='rejected', reviewed_by=$1::uuid, reviewed_at=NOW(),
                review_notes=$2
            WHERE id=$3::uuid
            """,
            admin.user_id, body.review_notes, proposal_id,
        )
    return {"ok": True}
