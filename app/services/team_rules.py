"""An owner decision is a cabinet transaction; memory text is never a ballot."""

import json
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException

from app.models.team_rules import Answer, Case, Feedback, Rule, Start

DEMO = Case(
    title="Порядок просмотра демонстрационных материалов",
    project="cognitive_core",
    team="Демонстрационная команда",
    role="Участник просмотра",
    conditions=["Демонстрация"],
    agents=["*"],
    rules=[
        Rule(address="9", choice="diagram_first", weight=8, reason="Начать с общей схемы"),
        Rule(address="10", choice="table_first", weight=8, reason="Начать с табличных данных"),
    ],
)
OPTIONS = {"diagram_first": "Сначала схема, затем таблица", "table_first": "Сначала таблица, затем схема"}


def case_key(owner, spec):
    scope = [spec.project, spec.team, spec.role, spec.action, sorted(spec.conditions)]
    return uuid5(UUID(str(owner)), json.dumps(scope, ensure_ascii=False))


def card(key, spec):
    return {"case_id": str(key), **spec.model_dump(exclude={"agents", "rules"})}


async def register(conn, owner, spec):
    key = case_key(owner, spec)
    await conn.execute(
        """INSERT INTO team_rule_cases(id,owner_id,spec) VALUES($1,$2,$3)
                          ON CONFLICT(id) DO NOTHING""",
        key,
        owner,
        spec.model_dump_json(),
    )
    old = await conn.fetchval("SELECT spec FROM team_rule_cases WHERE id=$1 AND owner_id=$2", key, owner)
    if old is None or json.loads(old) != spec.model_dump():
        raise HTTPException(409, "Для этой области уже зарегистрирован другой набор правил.")
    return key


async def discover(conn, owner, agent):
    demo_key = case_key(owner, DEMO)
    items = [card(demo_key, DEMO)]
    rows = await conn.fetch(
        """SELECT id,spec FROM team_rule_cases WHERE owner_id=$1 AND id<>$2
                               AND ((spec->'agents') ? $3 OR (spec->'agents') ? '*') ORDER BY created_at""",
        owner,
        demo_key,
        agent,
    )
    items.extend(card(r["id"], Case.model_validate_json(r["spec"])) for r in rows)
    return {
        "cases": items,
        "next_tool": "cognitive_resolve_team_rules",
        "instruction": "Выберите подходящую область. Новый экземпляр просмотра получает новый UUID. " "Для повтора или продолжения того же экземпляра сохраните прежний instance_id.",
    }


async def locked_case(conn, owner, case_id, agent=None):
    row = await conn.fetchrow("SELECT spec FROM team_rule_cases WHERE id=$1 AND owner_id=$2 FOR UPDATE", case_id, owner)
    if not row:
        raise HTTPException(404, "Ситуация не найдена.")
    spec = Case.model_validate_json(row["spec"])
    if agent is not None and "*" not in spec.agents and agent not in spec.agents:
        raise HTTPException(404, "Ситуация не найдена.")
    return spec


async def rules_at(conn, key, spec):
    rules = {}
    additions = await conn.fetch("SELECT rule,created_at FROM team_rule_refinements WHERE case_id=$1 ORDER BY created_at,address", key)
    declared = [(r, None) for r in spec.rules]
    declared.extend((Rule.model_validate_json(row["rule"]), row["created_at"].isoformat()) for row in additions)
    for r, created in declared:
        rule_id = str(uuid5(key, r.address))
        rules[rule_id] = {"rule_id": rule_id, **r.model_dump(), "version": 1, "refined_at": created}
    # Each accepted feedback has the resulting version. Read only the newest for each rule.
    rows = await conn.fetch(
        """SELECT DISTINCT ON (payload->>'rule_id') receipt FROM team_rule_feedback
        WHERE case_id=$1 ORDER BY payload->>'rule_id', (receipt->>'version')::int DESC""",
        key,
    )
    for row in rows:
        event = json.loads(row["receipt"])
        rules[event["rule_id"]].update(weight=event["new_weight"], version=event["version"])
    return rules


def assess(rules):
    parents = {r["address"].rpartition(".")[0] for r in rules.values()}
    leaves = [r for r in rules.values() if r["address"] not in parents]
    required = [r for r in leaves if r["required"]]
    top = required or [r for r in leaves if r["weight"] == max(n["weight"] for n in leaves)]
    choices = {r["choice"] for r in top}
    return {"rules": list(rules.values()), "applied_rule_ids": [r["rule_id"] for r in top], "choice": choices.pop() if len(choices) == 1 else None}


def result(run, spec):
    decided = run["choice"] is not None
    view = {
        "case": card(run["case_id"], spec),
        "conflict_id": str(run["id"]),
        "instance_id": str(run["instance_id"]),
        "opened_by": run["agent"],
        "opened_at": run["created_at"],
        "decision_source": {"future": "owner_cabinet", "once": "owner_cabinet", "inherited": "prior_owner_decision", "rules": "applicable_rules"}.get(run["duration"]),
        "assessment": json.loads(run["assessment"]),
        "options": OPTIONS,
        "status": "resolved" if decided else "pending_owner",
        "choice": run["choice"],
        "duration": run["duration"],
        "source_id": str(run["source_id"]) if run["source_id"] else None,
        "revoked_at": run["revoked_at"],
        "decided_at": run["decided_at"],
        "owner_url": f"https://mcp.me-ai.ru/ui/team-decisions?conflict={run['id']}",
    }
    if not decided:
        view["question"] = "Сначала схема или сначала таблица? Только сейчас или и на будущее?"
    return view


async def resolve(conn, owner, agent, body: Start):
    if body.case_id == case_key(owner, DEMO):
        await register(conn, owner, DEMO)
    spec = await locked_case(conn, owner, body.case_id, agent)
    run = await conn.fetchrow("SELECT * FROM team_rule_runs WHERE case_id=$1 AND instance_id=$2", body.case_id, body.instance_id)
    created = run is None
    if created:
        assessment = assess(await rules_at(conn, body.case_id, spec))
        run = await conn.fetchrow(
            """INSERT INTO team_rule_runs(id,case_id,instance_id,agent,assessment)
            VALUES($1,$2,$3,$4,$5) RETURNING *""",
            uuid4(),
            body.case_id,
            body.instance_id,
            agent,
            json.dumps(assessment),
        )
    if run["choice"] is None:
        future = await conn.fetchrow(
            """SELECT id,choice FROM team_rule_runs
            WHERE case_id=$1 AND duration='future' AND revoked_at IS NULL""",
            body.case_id,
        )
        selected = future["choice"] if future else json.loads(run["assessment"])["choice"]
        if selected:
            run = await conn.fetchrow(
                """UPDATE team_rule_runs SET choice=$2,duration=$3,source_id=$4,
                decided_at=now() WHERE id=$1 RETURNING *""",
                run["id"],
                selected,
                "inherited" if future else "rules",
                future["id"] if future else None,
            )
    return {**result(run, spec), "ask_owner": created and run["choice"] is None}


async def find_run(conn, owner, run_id):
    key = await conn.fetchval(
        """SELECT c.id FROM team_rule_cases c JOIN team_rule_runs r ON r.case_id=c.id
                                 WHERE r.id=$1 AND c.owner_id=$2""",
        run_id,
        owner,
    )
    if key is None:
        raise HTTPException(404, "Решение не найдено.")
    spec = await locked_case(conn, owner, key)
    return spec, await conn.fetchrow("SELECT * FROM team_rule_runs WHERE id=$1 AND case_id=$2", run_id, key)


async def answer(conn, owner, run_id, body: Answer):
    spec, run = await find_run(conn, owner, run_id)
    if run["choice"] is not None:
        if (run["choice"], run["duration"]) != (body.choice, body.duration):
            raise HTTPException(409, "Для этого просмотра уже есть другое решение.")
        return result(run, spec)
    active = await conn.fetchval(
        """SELECT id FROM team_rule_runs WHERE case_id=$1 AND duration='future'
                                    AND revoked_at IS NULL""",
        run["case_id"],
    )
    if active:
        raise HTTPException(409, "Появилось постоянное решение. Агенту нужно повторить проверку.")
    run = await conn.fetchrow(
        """UPDATE team_rule_runs SET choice=$2,duration=$3,decided_at=now()
                                 WHERE id=$1 RETURNING *""",
        run_id,
        body.choice,
        body.duration,
    )
    return result(run, spec)


async def revoke(conn, owner, run_id):
    spec, run = await find_run(conn, owner, run_id)
    if run["duration"] != "future":
        raise HTTPException(409, "Можно отозвать только решение на будущее.")
    run = await conn.fetchrow(
        """UPDATE team_rule_runs SET revoked_at=COALESCE(revoked_at,now())
                                 WHERE id=$1 RETURNING *""",
        run_id,
    )
    return result(run, spec)


async def refine(conn, owner, case_id, rule: Rule):
    spec = await locked_case(conn, owner, case_id)
    rules = await rules_at(conn, case_id, spec)
    rid = str(uuid5(case_id, rule.address))
    existing = await conn.fetchval("SELECT rule FROM team_rule_refinements WHERE case_id=$1 AND address=$2", case_id, rule.address)
    if existing and Rule.model_validate_json(existing) == rule:
        return rules[rid]
    if rid in rules:
        raise HTTPException(409, "Этот адрес занят другим правилом.")
    parent = rules.get(str(uuid5(case_id, rule.address.rpartition(".")[0])))
    if not parent or parent["weight"] != 10 or parent["required"] != rule.required or not set(parent["when"]) < set(rule.when) <= set(spec.conditions) or len(rules) >= 32:
        raise HTTPException(422, "Нужен родитель веса 10 и более узкое условие внутри этой области.")
    await conn.execute("INSERT INTO team_rule_refinements(case_id,address,rule) VALUES($1,$2,$3)", case_id, rule.address, rule.model_dump_json())
    return (await rules_at(conn, case_id, spec))[rid]


async def feedback(conn, owner, case_id, body: Feedback):
    spec = await locked_case(conn, owner, case_id)
    old = await conn.fetchrow("SELECT payload,receipt FROM team_rule_feedback WHERE case_id=$1 AND id=$2", case_id, body.id)
    if old:
        if json.loads(old["payload"]) != body.model_dump(mode="json"):
            raise HTTPException(409, "Этот ID отзыва уже использован с другим содержанием.")
        return json.loads(old["receipt"])
    duplicate = await conn.fetchval(
        """SELECT id FROM team_rule_feedback WHERE case_id=$1
        AND payload->>'rule_id'=$2 AND payload->>'evidence_run_id'=$3""",
        case_id,
        str(body.rule_id),
        str(body.evidence_run_id),
    )
    if duplicate:
        raise HTTPException(409, f"Этот просмотр уже учтён для правила. Квитанция {duplicate}")
    evidence = await conn.fetchrow("SELECT assessment,choice FROM team_rule_runs WHERE id=$1 AND case_id=$2", body.evidence_run_id, case_id)
    if not evidence or evidence["choice"] is None or str(body.rule_id) not in json.loads(evidence["assessment"])["applied_rule_ids"]:
        raise HTTPException(422, "Нужен завершённый просмотр с этим правилом.")
    rule = (await rules_at(conn, case_id, spec)).get(str(body.rule_id))
    if not rule:
        raise HTTPException(404, "Правило не найдено.")
    receipt = {
        **body.model_dump(mode="json"),
        "case_id": str(case_id),
        "address": rule["address"],
        "old_weight": rule["weight"],
        "new_weight": max(1, min(10, rule["weight"] + body.delta)),
        "version": rule["version"] + 1,
        "source": "owner_cabinet",
        "owner_id": str(owner),
    }
    await conn.execute("INSERT INTO team_rule_feedback(case_id,id,payload,receipt) VALUES($1,$2,$3,$4)", case_id, body.id, body.model_dump_json(), json.dumps(receipt))
    return receipt


async def history(conn, owner, case_id, agent, *, before=None):
    if case_id == case_key(owner, DEMO):
        spec = DEMO
    else:
        spec = await locked_case(conn, owner, case_id, agent)
    rows = await conn.fetch(
        """SELECT * FROM team_rule_runs WHERE case_id=$1
        AND ($2::uuid IS NULL OR (created_at,id)<(SELECT created_at,id FROM team_rule_runs WHERE id=$2 AND case_id=$1))
        ORDER BY created_at DESC,id DESC LIMIT 31""",
        case_id,
        before,
    )
    events = await conn.fetch(
        """SELECT receipt,created_at FROM team_rule_feedback WHERE case_id=$1
                                 ORDER BY created_at DESC,id DESC LIMIT 30""",
        case_id,
    )
    return {
        "case": card(case_id, spec),
        "rules": list((await rules_at(conn, case_id, spec)).values()),
        "decisions": [result(row, spec) for row in rows[:30]],
        "next_before": str(rows[29]["id"]) if len(rows) > 30 else None,
        "recent_feedback": [{**json.loads(e["receipt"]), "created_at": e["created_at"]} for e in events],
    }
