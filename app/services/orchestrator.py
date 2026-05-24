"""Cognitive Orchestrator — диспетчер задач от owner-а другим помощникам.

ARCHITECTURE
============

Orchestrator живёт как отдельный daemon (см. scripts/cogcore-orchestrator-daemon.py).
Он зарегистрирован в системе как обычный agent с agent_id="orchestrator" и своим
API-key. Цикл работы:

  1. Polls /agents/inbox каждые poll_interval секунд (default 5s)
  2. Для каждого нового DM (от owner или другого agent):
     - Скармливает текст DeepSeek с system_prompt + список whitelisted actions
     - DeepSeek возвращает JSON {action, args, reasoning, confidence}
  3. Если action в списке destructive → request_approval (DM owner-у с YES/NO,
     ждёт ответа 5 минут)
  4. Иначе → execute_action немедленно
  5. Отвечает источнику DM-ом с результатом
  6. Логирует решение в L1 (domain=orchestrator_decisions)

Этот модуль содержит:
  - OrchestratorConfig — конфиг
  - parse_command()    — pure function: DeepSeek-парсинг команды в action
  - execute_action()   — pure function: dispatch action к нужному API call
  - SYSTEM_PROMPT      — описание для DeepSeek
  - ACTION_SCHEMA      — whitelist валидных actions

Standalone runner живёт в scripts/cogcore-orchestrator-daemon.py чтобы НЕ
требовать пересборки cognitive_api контейнера при изменении логики оркестратора.

DESIGN NOTES
============

1. **Action whitelist hardcoded** — НЕ доверяем DeepSeek классификации
   destructive vs safe. Любая команда не из списка → отвергается.

2. **Approval gate**: actions в DESTRUCTIVE_ACTIONS требуют explicit "YES" от
   owner-а в течение 5 минут, иначе abort. Owner определяется через
   owner_bootstrap_email из конфига или явно передаётся.

3. **Anti-loop**: если source_agent == "orchestrator" — игнорируется
   (защита от того что orchestrator ответит сам себе).

4. **SQL injection safe**: payload в L1 идёт как JSON (asyncpg jsonb),
   никакого SQL string concat. Но в `lessons` поле cognitive_remember есть
   фильтр на `--`/`;`/SQL keywords — orchestrator пишет результат в
   `task` поле (длинное), `result` (короткое), без lessons (там фильтр).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("orchestrator")


# ─── Action whitelist ─────────────────────────────────────────────────────
#
# Каждое действие имеет:
#   - args:    обязательные/опциональные параметры
#   - destructive: требует ли YES от owner-а перед выполнением
#   - description: для system_prompt
#
# Если DeepSeek вернёт action которого нет в этом словаре — отказ.
#
# IMPORTANT: добавляя новый action, ОБЯЗАТЕЛЬНО оцените destructive флаг.
# Любое действие меняющее данные без возможности undo → destructive=True.

ACTIONS: dict[str, dict[str, Any]] = {
    # ─── Read-only / informational ───
    "query_status": {
        "destructive": False,
        "description": "Показать статус всех агентов (online/offline, last_seen, current_task).",
        "args": {},
    },
    "query_agent_state": {
        "destructive": False,
        "description": "Получить state конкретного agent-а (state, last checkpoint, recent events).",
        "args": {"agent_id": "str — agent_id"},
    },
    "list_inbox": {
        "destructive": False,
        "description": "Просмотр последних N сообщений в моём inbox (для self-debug).",
        "args": {"limit": "int 1..50 (default 10)"},
    },
    "ping_agent": {
        "destructive": False,
        "description": "Послать тестовый DM агенту и сообщить о результате.",
        "args": {"agent_id": "str — кому пинговать", "text": "str — текст пинга"},
    },

    # ─── Communications ───
    "send_dm": {
        "destructive": False,
        "description": "Передать сообщение/задачу другому agent-у через DM.",
        "args": {"to": "str — agent_id получателя", "text": "str — текст сообщения"},
    },
    "broadcast": {
        "destructive": False,
        "description": "Разослать одно и то же сообщение нескольким agent-ам.",
        "args": {
            "to": "list[str] — список agent_id",
            "text": "str — текст сообщения",
        },
    },
    "room_post": {
        "destructive": False,
        "description": "Опубликовать сообщение в room через room_key.",
        "args": {"room_key": "str", "text": "str"},
    },

    # ─── Knowledge / memory (safe) ───
    "remember_fact": {
        "destructive": False,
        "description": "Записать факт в долгосрочную память L1 (domain указывается).",
        "args": {"domain": "str", "task": "str", "result": "str (optional)"},
    },

    # ─── Destructive (require approval) ───
    "delete_agent": {
        "destructive": True,
        "description": "Удалить агента (агент перестанет приниматься API).",
        "args": {"agent_id": "str"},
    },
    "revoke_key": {
        "destructive": True,
        "description": "Отозвать конкретный api_key (агент потеряет доступ).",
        "args": {"api_key": "str (полный 64-hex ключ)"},
    },
    "mass_dm": {
        "destructive": True,
        "description": "Разослать >5 agent-ам — рассматривается как массовая рассылка.",
        "args": {"to": "list[str]", "text": "str"},
    },
    "purge_data": {
        "destructive": True,
        "description": "Удалить данные из L1/L2/L3 (любая purge-операция).",
        "args": {"domain": "str", "older_than_days": "int >= 30"},
    },

    # ─── Refusal sentinel ───
    "refuse": {
        "destructive": False,
        "description": "Я не могу выполнить эту команду (не понял / нет прав / out of scope). Объясни owner-у на русском.",
        "args": {"reason": "str — почему отказ"},
    },
    "request_clarification": {
        "destructive": False,
        "description": "Команда неоднозначная — задай уточняющий вопрос owner-у.",
        "args": {"question": "str — что уточнить"},
    },
}


DESTRUCTIVE_ACTIONS = frozenset(name for name, spec in ACTIONS.items() if spec["destructive"])


def build_system_prompt(orchestrator_id: str = "orchestrator") -> str:
    """System prompt для DeepSeek — описание роли + whitelisted actions."""
    actions_doc = []
    for name, spec in ACTIONS.items():
        flag = " (DESTRUCTIVE → требует approval owner-а)" if spec["destructive"] else ""
        args_doc = ", ".join(f"{k}: {v}" for k, v in spec["args"].items()) or "нет аргументов"
        actions_doc.append(f"  - {name}{flag}: {spec['description']} args: {{{args_doc}}}")
    actions_block = "\n".join(actions_doc)

    return f"""Ты — Orchestrator (agent_id={orchestrator_id}) в системе Cognitive Core.
Твоя роль — принимать команды от owner-а и других агентов на естественном русском
языке и преобразовывать их в одно из whitelisted действий. Ты НЕ выполняешь действия
напрямую — ты только классифицируешь намерение и подбираешь параметры.

ВАЖНЫЕ ПРАВИЛА:
1. Отвечай СТРОГО валидным JSON. Никакого markdown, никаких комментариев. Только {{...}}.
2. Поля JSON: action (одно из списка ниже), args (объект), reasoning (краткое объяснение
   почему ты выбрал именно это), confidence (0.0..1.0).
3. Если команда непонятна или неоднозначна → action="request_clarification" с question.
4. Если команда явно вне списка (например, «сгенерируй стихотворение») → action="refuse".
5. Никогда не угадывай agent_id — если в сообщении не указано чёткое имя, используй
   request_clarification и спроси у owner-а.
6. Для destructive actions (delete_agent, revoke_key, mass_dm, purge_data) — ты ВСЁ
   РАВНО возвращаешь это action в JSON. Approval gate срабатывает на стороне runtime,
   не на стороне твоей классификации.

ДОСТУПНЫЕ ДЕЙСТВИЯ:
{actions_block}

ПРИМЕРЫ:

Owner: «статус всех агентов»
→ {{"action":"query_status","args":{{}},"reasoning":"запрос на список и состояние агентов","confidence":0.95}}

Owner: «передай растру задачу подготовить отчёт»
→ {{"action":"send_dm","args":{{"to":"rastr","text":"Подготовь отчёт"}},"reasoning":"forward задачи агенту rastr","confidence":0.85}}

Owner: «удали тестового агента test-bot»
→ {{"action":"delete_agent","args":{{"agent_id":"test-bot"}},"reasoning":"запрос на удаление; destructive","confidence":0.9}}

Owner: «кто онлайн?»
→ {{"action":"query_status","args":{{}},"reasoning":"запрос на список онлайн агентов","confidence":0.95}}

Owner: «передай ему задачу»
→ {{"action":"request_clarification","args":{{"question":"Кому именно передать задачу? Укажи agent_id."}},"reasoning":"нет указания получателя","confidence":0.95}}

Owner: «напиши стих про осень»
→ {{"action":"refuse","args":{{"reason":"Я диспетчер задач между агентами, не могу генерировать художественный контент. Попроси у другого помощника."}},"reasoning":"вне моей роли","confidence":0.9}}

Owner: «отзови ключ abc123def456...»
→ {{"action":"revoke_key","args":{{"api_key":"abc123def456..."}},"reasoning":"запрос на revoke; destructive","confidence":0.9}}

ОТВЕЧАЙ ТОЛЬКО JSON, БЕЗ ПОЯСНЕНИЙ."""


def parse_llm_json(raw: str) -> dict:
    """Извлекает JSON из ответа LLM (с защитой от markdown-обёртки)."""
    text = raw.strip()
    # Strip markdown code fence
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Find first {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    snippet = text[start:end + 1]
    return json.loads(snippet)


def validate_action(parsed: dict) -> dict:
    """Проверяет что parsed result содержит валидный action и достаточно args.

    Возвращает dict {action, args, reasoning, confidence, valid:bool, error}.
    Если invalid — действие подменяется на 'refuse' с описанием проблемы.
    """
    action = parsed.get("action") or ""
    args = parsed.get("args") or {}
    reasoning = parsed.get("reasoning") or ""
    confidence = float(parsed.get("confidence") or 0.0)

    if action not in ACTIONS:
        return {
            "action": "refuse",
            "args": {"reason": f"Unknown action '{action}' — нет в whitelist."},
            "reasoning": reasoning,
            "confidence": confidence,
            "valid": False,
            "error": f"unknown_action:{action}",
        }

    # Проверка обязательных args по схеме (heuristic — обязательные те у кого
    # нет слова "optional" в description ACTIONS)
    spec_args = ACTIONS[action]["args"]
    missing = []
    for k, descr in spec_args.items():
        if "optional" in (descr or "").lower():
            continue
        if k not in args or args[k] in (None, ""):
            missing.append(k)
    if missing:
        return {
            "action": "request_clarification",
            "args": {"question": f"Для действия {action} не хватает: {', '.join(missing)}. Уточни, пожалуйста."},
            "reasoning": reasoning,
            "confidence": confidence,
            "valid": False,
            "error": f"missing_args:{','.join(missing)}",
        }

    return {
        "action": action,
        "args": args,
        "reasoning": reasoning,
        "confidence": confidence,
        "valid": True,
        "error": None,
    }


def is_destructive(action: str) -> bool:
    return action in DESTRUCTIVE_ACTIONS


def expand_mass_dm_threshold(action: str, args: dict) -> str:
    """broadcast → mass_dm если получателей больше 5."""
    if action == "broadcast":
        to = args.get("to") or []
        if isinstance(to, list) and len(to) > 5:
            return "mass_dm"
    return action


# ─── L1 logging helpers ───────────────────────────────────────────────────


def decision_payload(
    source_agent: str,
    source_text: str,
    parsed: dict,
    *,
    requires_approval: bool,
    approval_status: str = "not_required",
    execution_result: dict | None = None,
) -> dict:
    """Формирует payload для записи в L1 (domain=orchestrator_decisions)."""
    return {
        "source_agent": source_agent,
        "source_text": source_text[:1500],
        "action": parsed.get("action"),
        "args": parsed.get("args") or {},
        "reasoning": (parsed.get("reasoning") or "")[:500],
        "confidence": parsed.get("confidence"),
        "requires_approval": requires_approval,
        "approval_status": approval_status,
        "execution_result": execution_result,
    }


def sanitize_for_remember(text: str) -> str:
    """L1 API filter блокирует '--', ';', SQL keywords в lessons/tools_used.

    Эта функция превращает запрещённые символы в безопасные эквиваленты
    (em-dash вместо --, '.' вместо ;). Используется только если хотим
    записать в lessons; для task/result поля фильтр обычно мягче.
    """
    if not text:
        return ""
    # Replace SQL injection markers
    text = text.replace("--", "—")  # em-dash
    text = text.replace(";", ".")
    # Strip SQL keywords (case-insensitive)
    for kw in ("UNION", "DROP", "SELECT", "INSERT", "DELETE", "UPDATE", "ALTER"):
        text = re.sub(rf"\b{kw}\b", kw.lower().title(), text, flags=re.IGNORECASE)
    return text


# ─── Anti-loop check ──────────────────────────────────────────────────────


def should_ignore_message(msg: dict, *, orchestrator_id: str) -> bool:
    """Anti-loop: игнорируем DM от себя самого + автоматические heartbeat-уведомления.

    msg должен иметь keys: from, text, context.
    """
    sender = msg.get("from") or ""
    if sender == orchestrator_id:
        return True
    # server-runtime auto-DM (e.g. agent_joined) — нам ничего делать не нужно
    if sender == "server-runtime":
        return True
    text = (msg.get("text") or "").lower()
    # System markers — пропускаем
    if "[from " in text and "server-runtime" in text:
        return True
    return False


# ─── Owner detection ──────────────────────────────────────────────────────


@dataclass
class OrchestratorConfig:
    """Configuration for orchestrator runtime."""
    api_base: str = "http://127.0.0.1:8000"
    orchestrator_api_key: str = ""
    orchestrator_id: str = "orchestrator"
    owner_agent_id: str = ""  # agent_id который рассматривается как owner для approval-flow
    owner_email: str = ""     # для будущей логики (e.g. отправка email approval'а)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    poll_interval_seconds: int = 5
    approval_timeout_seconds: int = 300  # 5 минут
    rate_limit_send_per_min: int = 30    # safety margin под server-side 60/min
    log_decisions_to_l1: bool = True


def is_owner_message(msg: dict, cfg: OrchestratorConfig) -> bool:
    """Определяет является ли DM от owner-а.

    Логика: если sender совпадает с cfg.owner_agent_id — это owner.
    """
    sender = msg.get("from") or ""
    if cfg.owner_agent_id and sender == cfg.owner_agent_id:
        return True
    return False
