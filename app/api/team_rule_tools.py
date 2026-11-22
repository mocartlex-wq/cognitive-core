"""MCP agents discover and consult the ledger. No owner mutation is exposed."""

import json
from uuid import UUID

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.db.postgres import get_pool
from app.models.team_rules import Start, Strict
from app.services import team_rules


class Listing(Strict):
    pass


class History(Strict):
    case_id: UUID
    before: UUID | None = None
    feedback_id: UUID | None = None


CATALOG = {
    "cognitive_team_rules": (Listing, "Какие командные правила действуют для просмотра схемы и таблицы? " "Найти ситуации и область действия по ключу агента. Начните без ID."),
    "cognitive_resolve_team_rules": (
        Start,
        "Узнать порядок просмотра материалов для конкретного действия. "
        "Получите case_id из cognitive_team_rules. Новый просмотр — новый instance_id. "
        "При ask_owner покажите оба варианта и срока, рекомендуйте порядок и срок по цели задачи "
        "и объясните, когда лучше другой вариант. Приложите owner_url. Если владелец уже назвал "
        "вариант и срок в чате, передайте их в chat_answer с прежним instance_id. "
        "Отправьте confirmation_url для личного подтверждения. Рекомендация агента не является ответом. "
        "До подтверждения решение ожидает владельца. Проверяйте тот же instance_id без chat_answer. "
        "При resolved примените choice в своей работе.",
    ),
    "cognitive_team_rule_history": (History, "Прочитать действующие веса и историю решений, причин и отзывов " "для своей ситуации. Конкретный отзыв открывается по feedback_id."),
}
TOOLS = [{"name": name, "description": description, "inputSchema": model.model_json_schema()} for name, (model, description) in CATALOG.items()]


async def dispatch(name, args, owner, agent):
    if not owner:
        return {"_error": "Нужен ключ агента, привязанный к владельцу.", "_status": 403}
    body = CATALOG[name][0].model_validate(args)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            if name == "cognitive_team_rules":
                data = await team_rules.discover(conn, owner, agent)
            elif name == "cognitive_resolve_team_rules":
                data = await team_rules.resolve(conn, owner, agent, body)
            elif body.feedback_id:
                await team_rules.locked_case(conn, owner, body.case_id, agent)
                event = await conn.fetchrow("SELECT receipt,created_at FROM team_rule_feedback WHERE case_id=$1 AND id=$2", body.case_id, body.feedback_id)
                if not event:
                    raise HTTPException(404, "Отзыв не найден.")
                data = {**json.loads(event["receipt"]), "created_at": event["created_at"]}
            else:
                data = await team_rules.history(conn, owner, body.case_id, agent, before=body.before)
    except HTTPException as exc:
        return {"_error": exc.detail, "_status": exc.status_code}
    return jsonable_encoder(data)
