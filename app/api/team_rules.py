"""Only an authenticated owner in the cabinet can choose, revoke or give feedback."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db.postgres import get_pool
from app.models.team_rules import Answer, Case, Feedback, Rule
from app.security.middleware import require_user
from app.security.session import SESSION_COOKIE_NAME
from app.security.session_origin import require_same_origin_write
from app.services import team_rules as rules

router = APIRouter(prefix="/user/team-decisions", tags=["user"])


async def cabinet(request: Request):
    if not request.cookies.get(SESSION_COOKIE_NAME):
        raise HTTPException(401, "Войдите в кабинет владельца.")
    user = await require_user(request)
    if request.method != "GET":
        require_same_origin_write(request)
    return user.user_id


@router.post("/cases")
async def create_case(body: Case, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if rules.case_key(owner, body) == rules.case_key(owner, rules.DEMO) and body != rules.DEMO:
            raise HTTPException(409, "Область демонстрационного примера зарезервирована.")
        return rules.card(await rules.register(conn, owner, body), body)


@router.post("/cases/{case_id}/refinements")
async def refine_rule(case_id: UUID, body: Rule, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await rules.refine(conn, owner, case_id, body)


@router.post("/cases/{case_id}/feedback")
async def record_feedback(case_id: UUID, body: Feedback, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await rules.feedback(conn, owner, case_id, body)


@router.get("")
async def recent(owner=Depends(cabinet)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT r.id,r.duration,r.choice,r.created_at,c.spec->>'title' AS title
        FROM team_rule_cases c JOIN team_rule_runs r ON r.case_id=c.id WHERE c.owner_id=$1
        ORDER BY r.created_at DESC,r.id DESC LIMIT 50""",
        owner,
    )
    return {"items": [dict(row) for row in rows]}


@router.get("/{run_id}")
async def get_decision(run_id: UUID, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        spec, run = await rules.find_run(conn, owner, run_id)
        return rules.result(run, spec)


@router.post("/{run_id}/answer")
async def choose(run_id: UUID, body: Answer, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await rules.answer(conn, owner, run_id, body)


@router.post("/{run_id}/revoke")
async def withdraw(run_id: UUID, owner=Depends(cabinet)):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await rules.revoke(conn, owner, run_id)
