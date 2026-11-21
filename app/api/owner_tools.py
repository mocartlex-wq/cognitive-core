"""The signed-in owner's catalog, backed by the same registry the agents recall."""
import json
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.postgres import get_pool
from app.security.middleware import Session, require_user
from app.security.session_origin import require_same_origin_write
from app.services.embedder import embed_text
from app.services.operative import _vec_to_pg
from app.services.shared_tools import CARD_COLUMNS, CREDENTIAL_PATTERN, card_usage, read_shared_tool, resource_card

router = APIRouter(prefix="/user/shared-tools", tags=["user"])


class SharedTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    kind: Literal["skill", "script", "prompt", "service", "library", "api"]
    description: str = Field(min_length=1, max_length=3000)
    instructions: str = Field(min_length=1, max_length=64000)
    scope: Literal["portable", "local", "service"]
    location: str = Field(default="", max_length=1500)

    @field_validator("name", "description", "location", mode="before")
    @classmethod
    def trim_label(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("instructions")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("Нужен текст ресурса или инструкция.")
        return value


@router.get("")
async def list_shared_tools(q: str = Query("", max_length=300), offset: int = Query(0, ge=0),
                            limit: int = Query(20, ge=1, le=100), user: Session = Depends(require_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, domain, tool_name, tool_type, description,
                  jsonb_build_object('scope', usage_patterns->'scope',
                      'local_path', usage_patterns->'local_path', 'resource_kind', usage_patterns->'resource_kind')
                    AS usage_patterns FROM l3_tools_registry
            WHERE owner_user_id = $1::uuid AND effective_to IS NULL
              AND ($2::text = '' OR strpos(lower(concat_ws(' ', tool_name, domain, tool_type,
                      description, usage_patterns::text)), lower($2)) > 0)
            ORDER BY lower(tool_name), id OFFSET $3 LIMIT $4""",
        user.user_id, q.strip(), offset, limit + 1,
    )
    return {"items": [resource_card(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/{tool_id}")
async def get_shared_tool(tool_id: UUID, user: Session = Depends(require_user)):
    card = await read_shared_tool(tool_id, user.user_id)
    if card is None:
        raise HTTPException(404, "Карточка не найдена.")
    return card


@router.post("")
async def add_shared_tool(body: SharedTool, request: Request, user: Session = Depends(require_user)):
    require_same_origin_write(request)
    for value in body.model_dump().values():
        if "\x00" in value or CREDENTIAL_PATTERN.search(value):
            raise HTTPException(422, "Удалите учётные данные или недопустимые символы из карточки.")
    if body.location and not re.match(r"^[A-Za-z]:[/\\]", body.location):
        try:
            url = urlsplit(body.location)
            if url.scheme and (url.scheme.lower() not in ("https", "http") or not url.hostname
                               or url.username is not None or url.password is not None):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Укажите путь или HTTP(S)-адрес без учётных данных.") from None
    kind = "prompt" if body.kind == "skill" else body.kind
    usage = {"resource_kind": body.kind, "scope": body.scope, "instructions": body.instructions}
    if body.location:
        usage["location"] = body.location
    # Owner-scoped recall uses pgvector. Persist the row and its vector in one statement.
    searchable = json.dumps({"name": body.name, "type": kind, "description": body.description,
                             "usage": usage}, ensure_ascii=False)
    vector = _vec_to_pg(await embed_text(searchable))
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""INSERT INTO l3_tools_registry
                    (domain, tool_name, tool_type, description, config_schema, usage_patterns, owner_user_id, embedding,
                     owner_managed)
                    VALUES ('skills', $1,$2,$3,'{{}}',$4::jsonb,$5::uuid,$6::vector,true)
                    ON CONFLICT (domain, tool_name, owner_user_id) WHERE effective_to IS NULL DO NOTHING
                    RETURNING {CARD_COLUMNS}""",
                body.name, kind, body.description, json.dumps(usage, ensure_ascii=False), user.user_id, vector,
            )
            created = row is not None
            if not created:
                row = await conn.fetchrow(f"""SELECT {CARD_COLUMNS} FROM l3_tools_registry
                                              WHERE domain='skills' AND tool_name=$1 AND owner_user_id=$2::uuid
                                                AND owner_managed AND effective_to IS NULL""", body.name, user.user_id)
                if not row or row["tool_type"] != kind or row["description"] != body.description or card_usage(row) != usage:
                    raise HTTPException(409, "В вашем каталоге уже есть другая карточка с таким названием.")
    return {"created": created, "item": resource_card(row, full=True)}
