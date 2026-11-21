"""The signed-in owner's original memory entries and private annotations."""
import base64
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.postgres import get_pool
from app.security.middleware import require_user
from app.security.session import Session
from app.security.session_origin import require_same_origin_write
from app.services.memory_records import read_record

router = APIRouter(prefix="/user/memory", tags=["user"])

# Older cognitive_remember rows have no kind. Recognize their actual schema,
# not every L1 event containing a "task" (e.g. operational/inbox messages).
VISIBLE = """e.owner_user_id = $1::uuid AND e.raw_payload ? 'task'
    AND jsonb_typeof(e.raw_payload->'task') = 'string'
    AND e.domain NOT IN ('agent_inbox', 'agent_lifecycle', 'rooms', 'room_messages')
    AND (e.raw_payload->>'kind' IN ('session_note', 'memory_document')
         OR (NOT e.raw_payload ? 'kind'
             AND e.raw_payload ?| ARRAY['result','lessons','feedback']
             AND e.raw_payload - ARRAY['task','result','lessons','feedback','tools_used','checkpoint',
                                       'session_id','chat_id']::text[] = '{}'::jsonb))"""
SESSION_ID = """COALESCE(
    CASE WHEN jsonb_typeof(e.raw_payload #> '{checkpoint,session_id}')='string'
         THEN NULLIF(e.raw_payload #>> '{checkpoint,session_id}', '') END,
    CASE WHEN jsonb_typeof(e.raw_payload->'session_id')='string' THEN NULLIF(e.raw_payload->>'session_id', '') END,
    CASE WHEN jsonb_typeof(e.raw_payload->'chat_id')='string' THEN NULLIF(e.raw_payload->>'chat_id', '') END)"""


async def _viewer(request: Request, response: Response) -> Session:
    response.headers["Cache-Control"] = "private, no-store"
    return await require_user(request)


class CommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, text: str) -> str:
        if "\x00" in text:
            raise ValueError("Недопустимый символ в комментарии")
        return text


def _decode_cursor(cursor):
    if cursor is None:
        return None, None
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True).decode("ascii")
        date, rid = raw.split("|")
        date = datetime.fromisoformat(date)
        if date.tzinfo is None:
            raise ValueError()
        return date, UUID(rid)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Неверный указатель страницы") from None


def _page(rows, limit, date_field):
    items = [dict(row) for row in rows[:limit]]
    cursor = None
    if len(rows) > limit:
        last = items[-1]
        raw = f"{last[date_field].isoformat()}|{last['id']}".encode("ascii")
        cursor = base64.urlsafe_b64encode(raw).decode("ascii")
    return {"items": items, "next_cursor": cursor}


def _field(name):
    # Nested structures (including arbitrary event payloads) are never returned as text.
    return f"CASE WHEN jsonb_typeof(e.raw_payload->'{name}')='string' THEN e.raw_payload->>'{name}' ELSE '' END"


async def _records(owner, *, limit, record_id=None, agent=None, session_id=None,
                   domain=None, q=None, cursor=None, preview=True):
    where = [VISIBLE]
    args = [owner]

    def arg(value):
        if isinstance(value, str) and "\x00" in value:
            raise HTTPException(422, "Недопустимый символ в фильтре")
        args.append(value)
        return f"${len(args)}"

    if record_id is not None:
        where.append(f"e.id={arg(record_id)}::uuid")
    if agent:
        term = arg(agent)
        where.append(f"(lower(e.source_agent)=lower({term}) OR lower(a.agent_label)=lower({term}))")
    if session_id:
        where.append(f"{SESSION_ID}={arg(session_id)}")
    if domain:
        where.append(f"e.domain={arg(domain)}")
    if q:
        term = arg(q)
        where.append("(" + " OR ".join(f"strpos(lower({_field(k)}), lower({term}))>0" for k in ("task", "result", "lessons")) + ")")
    if cursor is not None:
        ts, rid = _decode_cursor(cursor)
        where.append(f"(e.timestamp,e.id)<({arg(ts)}::timestamptz,{arg(rid)}::uuid)")

    fields = []
    truncated = []
    for key, size in (("task", 240), ("result", 700), ("lessons", 350)):
        val = _field(key)
        fields.append(f"left({val},{size}) AS {key}" if preview else f"{val} AS {key}")
        if preview:
            truncated.append(f"length({val})>{size}")

    sql = f"""
        SELECT e.id, e.timestamp, e.source_agent, a.agent_label, e.domain, {SESSION_ID} AS session_id,
               COALESCE(e.raw_payload->>'kind', CASE WHEN e.raw_payload->'checkpoint'->>'schema_version'='1'
                                                THEN 'checkpoint' ELSE 'memory' END) AS kind,
               CASE WHEN e.raw_payload #>> '{{checkpoint,status}}'
                    IN ('unknown','in_progress','blocked','awaiting_review','completed','cancelled')
                    THEN e.raw_payload #>> '{{checkpoint,status}}' END AS status,
               {', '.join(fields)}, ({' OR '.join(truncated) or 'false'}) AS truncated,
               (SELECT count(*) FROM owner_memory_comments c
                WHERE c.record_id=e.id AND c.owner_user_id=e.owner_user_id) AS comment_count
        FROM l1_raw_events e
        LEFT JOIN agent_states a ON a.agent_id=e.source_agent AND a.owner_user_id=e.owner_user_id
        WHERE {' AND '.join(where)}
        ORDER BY e.timestamp DESC, e.id DESC LIMIT {arg(limit + 1)}
    """
    pool = await get_pool()
    return _page(await pool.fetch(sql, *args), limit, "timestamp")


@router.get("/records")
async def list_records(user: Session = Depends(_viewer), record_id: UUID | None = None,
                       agent: str | None = Query(None, max_length=160),
                       session_id: str | None = Query(None, max_length=200),
                       domain: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=200),
                       limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, max_length=180)):
    return await _records(user.user_id, record_id=record_id, agent=agent, session_id=session_id,
                          domain=domain, q=q, limit=limit, cursor=cursor)


@router.get("/records/{record_id}")
async def record_detail(record_id: UUID, user: Session = Depends(_viewer)):
    page = await _records(user.user_id, record_id=record_id, limit=1, preview=False)
    if not page["items"]:
        raise HTTPException(404, "Запись не найдена")
    record = page["items"][0]
    pool = await get_pool()
    source = await read_record(pool, str(record_id), user.user_id) or {}
    checkpoint = source.get("checkpoint") or {}
    next_step = checkpoint.get("next_step")
    evidence = checkpoint.get("evidence")
    latest_id = source.get("latest_id")
    if latest_id and latest_id != str(record_id):
        visible = await pool.fetchval(f"SELECT 1 FROM l1_raw_events e WHERE {VISIBLE} AND e.id=$2::uuid",
                                      user.user_id, latest_id)
        if not visible:
            latest_id = None
    record.update(is_latest=source.get("is_latest"), latest_id=latest_id,
                  next_step=next_step[:2000] if isinstance(next_step, str) else None,
                  evidence=[item[:2000] for item in evidence[:20] if isinstance(item, str)] if isinstance(evidence, list) else [])
    return record


async def _require_record(conn, owner, record_id, *, lock=False):
    # Keep the source and its owner stable while adding or replaying an annotation.
    sql = f"SELECT 1 FROM l1_raw_events e WHERE {VISIBLE} AND e.id=$2"
    if lock:
        sql += " FOR UPDATE"
    if not await conn.fetchval(sql, owner, record_id):
        raise HTTPException(404, "Запись не найдена")


@router.get("/records/{record_id}/comments")
async def list_comments(record_id: UUID, user: Session = Depends(_viewer),
                        limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=180)):
    ts, rid = _decode_cursor(cursor)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _require_record(conn, user.user_id, record_id)
        rows = await conn.fetch("""
            SELECT id, text, created_at FROM owner_memory_comments
            WHERE record_id=$1 AND owner_user_id=$2
              AND ($3::timestamptz IS NULL OR (created_at,id)<($3,$4::uuid))
            ORDER BY created_at DESC,id DESC LIMIT $5
        """, record_id, user.user_id, ts, rid, limit + 1)
    return _page(rows, limit, "created_at")


@router.post("/records/{record_id}/comments")
async def add_comment(record_id: UUID, body: CommentInput, request: Request, user: Session = Depends(_viewer)):
    require_same_origin_write(request)
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _require_record(conn, user.user_id, record_id, lock=True)
        row = await conn.fetchrow("""
            INSERT INTO owner_memory_comments(record_id,owner_user_id,request_id,text)
            VALUES ($1,$2,$3,$4) ON CONFLICT (owner_user_id,request_id) DO NOTHING
            RETURNING id,text,created_at
        """, record_id, user.user_id, body.request_id, body.text)
        if row is not None:
            # Retention rechecks this predicate after a concurrent row lock.
            await conn.execute("""
                UPDATE l1_raw_events SET has_owner_comment=true
                WHERE id=$1 AND owner_user_id=$2 AND NOT has_owner_comment
            """, record_id, user.user_id)
        else:
            row = await conn.fetchrow("""
                SELECT id,text,created_at FROM owner_memory_comments
                WHERE owner_user_id=$1 AND request_id=$2 AND record_id=$3 AND text=$4
            """, user.user_id, body.request_id, record_id, body.text)
            if row is None:
                raise HTTPException(409, "Идентификатор повтора уже использован для другого комментария")
    return dict(row)
