#!/usr/bin/env python3
"""Cogcore Orchestrator Daemon — standalone runner.

Запускается systemd unit-ом `cogcore-orchestrator.service`. Не зависит от
cognitive_api container'а (отдельный python3 процесс на хосте). Это значит:
  - Любые изменения в этом файле НЕ требуют пересборки api image.
  - Скрипт перезапускается через `systemctl restart cogcore-orchestrator`.
  - Логи через journalctl: `journalctl -u cogcore-orchestrator -f`.

Этот daemon — клиент API. Все действия выполняются через публичные HTTP endpoints
(не прямой SQL!), что соответствует архитектурным границам.

ENV:
  - COGCORE_API_BASE     (default: http://127.0.0.1:9001 — host-side порт)
  - ORCHESTRATOR_API_KEY (required — выдан при register)
  - ORCHESTRATOR_AGENT_ID (default: orchestrator)
  - OWNER_AGENT_ID       (default: пусто — approval не работает)
  - DEEPSEEK_API_KEY     (required)
  - DEEPSEEK_BASE_URL    (default: https://api.deepseek.com/v1)
  - DEEPSEEK_MODEL       (default: deepseek-chat)
  - ORCH_POLL_INTERVAL_S (default: 5)
  - ORCH_APPROVAL_TIMEOUT_S (default: 300)
  - ORCH_LOG_LEVEL       (default: INFO)
  - ORCH_DRY_RUN         (default: 0 — если 1, action не выполняется, только log)

USAGE (manual / dev):
  ORCHESTRATOR_API_KEY=... DEEPSEEK_API_KEY=... python3 cogcore-orchestrator-daemon.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import shared orchestrator module — if `app.services.orchestrator` is on
# PYTHONPATH (we add repo root below), use it; иначе fail-fast.
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))

try:
    from app.services.orchestrator import (
        ACTIONS,
        DESTRUCTIVE_ACTIONS,
        OrchestratorConfig,
        build_system_prompt,
        decision_payload,
        expand_mass_dm_threshold,
        is_destructive,
        extract_plan,
        plan_has_destructive,
        substitute_step_placeholders,
        is_owner_message,
        parse_llm_json,
        sanitize_for_remember,
        should_ignore_message,
        validate_action,
    )
    # Phase 6: per-owner Agent Operating Rules injection
    from app.services.rules import fetch_rules_for_owner, build_rules_section
except ImportError as e:
    sys.stderr.write(f"Cannot import app.services.orchestrator from {_repo_root}: {e}\n")
    sys.stderr.write("Make sure script lives in <repo>/scripts/ and app/services/orchestrator.py exists.\n")
    sys.exit(2)

import httpx

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("ORCH_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cogcore-orchestrator")


# ─── Config from env ──────────────────────────────────────────────────────
def load_config() -> OrchestratorConfig:
    api_key = os.environ.get("ORCHESTRATOR_API_KEY", "").strip()
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        log.critical("ORCHESTRATOR_API_KEY is empty — cannot operate")
        sys.exit(3)
    if not ds_key:
        log.critical("DEEPSEEK_API_KEY is empty — cannot parse commands")
        sys.exit(3)
    return OrchestratorConfig(
        api_base=os.environ.get("COGCORE_API_BASE", "http://127.0.0.1:9001").rstrip("/"),
        orchestrator_api_key=api_key,
        orchestrator_id=os.environ.get("ORCHESTRATOR_AGENT_ID", "orchestrator"),
        owner_agent_id=os.environ.get("OWNER_AGENT_ID", "").strip(),
        owner_email=os.environ.get("OWNER_EMAIL", "").strip(),
        deepseek_api_key=ds_key,
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        poll_interval_seconds=int(os.environ.get("ORCH_POLL_INTERVAL_S", "5")),
        approval_timeout_seconds=int(os.environ.get("ORCH_APPROVAL_TIMEOUT_S", "300")),
        log_decisions_to_l1=os.environ.get("ORCH_LOG_TO_L1", "1") == "1",
    )


DRY_RUN = os.environ.get("ORCH_DRY_RUN", "0") == "1"


# ─── State files ──────────────────────────────────────────────────────────
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", "/var/run/cogcore-orchestrator"))
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    STATE_DIR = Path("/tmp/cogcore-orchestrator")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

PROCESSED_MARKER = STATE_DIR / "last_processed_id.txt"
PENDING_APPROVALS = STATE_DIR / "pending_approvals.json"


def read_last_processed_id() -> str:
    try:
        return PROCESSED_MARKER.read_text().strip()
    except FileNotFoundError:
        return ""


def write_last_processed_id(msg_id: str) -> None:
    try:
        PROCESSED_MARKER.write_text(msg_id.strip())
    except Exception as e:
        log.warning("cannot write last_processed marker: %s", e)


def read_pending_approvals() -> list[dict]:
    try:
        return json.loads(PENDING_APPROVALS.read_text())
    except FileNotFoundError:
        return []
    except Exception:
        return []


def write_pending_approvals(items: list[dict]) -> None:
    try:
        PENDING_APPROVALS.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("cannot persist pending approvals: %s", e)


# ─── HTTP helpers ─────────────────────────────────────────────────────────
class CogClient:
    """Thin wrapper вокруг cognitive_api endpoints."""

    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.api_base,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"X-API-Key": cfg.orchestrator_api_key},
        )

    async def close(self):
        await self._http.aclose()

    # ─ inbox + presence ─
    async def fetch_inbox(self, since_minutes: int = 5, limit: int = 50) -> list[dict]:
        r = await self._http.get(
            "/agents/inbox",
            params={"since_minutes": since_minutes, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("messages", [])

    async def fetch_online(self, within_seconds: int = 120) -> list[dict]:
        r = await self._http.get(
            "/agents/online",
            params={"within_seconds": within_seconds},
        )
        r.raise_for_status()
        return r.json().get("agents", [])

    async def list_all_agents(self) -> list[dict]:
        r = await self._http.get("/agents")
        r.raise_for_status()
        return r.json().get("items", [])

    async def get_agent_state(self, agent_id: str) -> dict:
        r = await self._http.get(f"/agents/{agent_id}/state")
        r.raise_for_status()
        return r.json()

    async def send_dm(self, to: str, text: str, context: dict | None = None) -> dict:
        r = await self._http.post(
            "/agents/message",
            json={"to": to, "text": text, "context": context or {}},
        )
        if r.status_code >= 400:
            log.warning("send_dm %s → HTTP %d: %s", to, r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json()

    async def heartbeat(self, current_task: str | None = None) -> None:
        try:
            await self._http.post(
                "/agents/heartbeat",
                json={"current_task": current_task or "polling inbox"},
            )
        except Exception as e:
            log.debug("heartbeat failed (non-fatal): %s", e)

    async def revoke_key(self, api_key: str) -> dict:
        r = await self._http.post("/agents/keys/revoke", json={"api_key": api_key})
        r.raise_for_status()
        return r.json()

    # ─ memory ─
    async def remember(
        self,
        domain: str,
        task: str,
        result: str = "",
        feedback: str = "",
        lessons: str = "",
    ) -> dict:
        """Через MCP endpoint cognitive_remember. Реальная запись в L1."""
        # Используем JSON-RPC к /mcp/messages — это переиспользует L1 schema
        # и при этом source_agent резолвится из api_key (то есть будет 'orchestrator')
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {
                "name": "cognitive_remember",
                "arguments": {
                    "domain": domain,
                    "task": task[:1900],
                    "result": result[:1900],
                    "feedback": feedback,
                    "lessons": sanitize_for_remember(lessons),
                },
            },
        }
        r = await self._http.post("/mcp/messages", json=payload)
        if r.status_code >= 400:
            log.warning("remember → HTTP %d: %s", r.status_code, r.text[:300])
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


# ─── DeepSeek ──────────────────────────────────────────────────────────────
class DeepSeekParser:
    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.deepseek_base_url,
            headers={"Authorization": f"Bearer {cfg.deepseek_api_key}"},
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    async def close(self):
        await self._http.aclose()

    async def parse(self, source_agent: str, text: str) -> dict:
        """Возвращает validated single-action dict (backward compat)."""
        raw = await self.parse_raw(source_agent, text)
        # Если raw уже refuse от error path — не валидируем повторно
        if raw.get("error") and not raw.get("valid", True):
            return raw
        return validate_action(raw)

    async def _resolve_owner_user_id(self, agent_id: str) -> str | None:
        """Phase 6: fetch owner_user_id для агента (для inject per-owner Operating Rules).

        Best-effort: при любой ошибке возвращает None — agent_runtime использует
        prompt без rules section (graceful degradation).
        """
        try:
            from app.db.postgres import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT owner_user_id::text AS uid FROM agent_states WHERE agent_id = $1",
                    agent_id,
                )
            return row["uid"] if row and row["uid"] else None
        except Exception:
            return None

    async def parse_raw(self, source_agent: str, text: str) -> dict:
        """Возвращает RAW LLM dict — может содержать 'action' (single) или 'plan' (multi).

        Для multi-step planning. Используется в process_message → extract_plan().
        При ошибке парсинга возвращает {action:'refuse', _parse_error:...}.

        Phase 6: инжектирует Operating Rules для owner'a source_agent в system_prompt.
        Если rules fetch fails (DB issue) — prompt всё равно строится без секции.
        """
        # Phase 6: inject per-owner Operating Rules
        rules_text = ""
        try:
            owner_uid = await self._resolve_owner_user_id(source_agent)
            if owner_uid:
                rules = await fetch_rules_for_owner(owner_uid)
                rules_text = build_rules_section(rules)
        except Exception:
            pass  # rules не блокируют LLM call — graceful degradation

        prompt = build_system_prompt(self.cfg.orchestrator_id, rules_text)
        user_msg = f"От {source_agent}: {text}"
        try:
            r = await self._http.post(
                "/chat/completions",
                json={
                    "model": self.cfg.deepseek_model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 800,
                },
            )
            r.raise_for_status()
            data = r.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            log.debug("DeepSeek raw: %s", content[:300])
            return parse_llm_json(content)
        except json.JSONDecodeError as e:
            log.warning("LLM JSON parse failed: %s; raw=%r", e, content if 'content' in locals() else None)
            return {
                "action": "refuse",
                "args": {"reason": "Не понял команду (parse error). Переформулируй?"},
                "reasoning": "json_parse_failed",
                "confidence": 0.0,
                "valid": False,
                "error": "json_parse_failed",
            }
        except httpx.HTTPError as e:
            log.error("DeepSeek HTTP failed: %s", e)
            return {
                "action": "refuse",
                "args": {"reason": "Не смог разобрать команду (LLM недоступен)."},
                "reasoning": "llm_unavailable",
                "confidence": 0.0,
                "valid": False,
                "error": str(e)[:200],
            }


# ─── Executor ──────────────────────────────────────────────────────────────
class Executor:
    """Исполнитель действий. Каждый метод соответствует action из ACTIONS."""

    def __init__(self, cfg: OrchestratorConfig, client: CogClient):
        self.cfg = cfg
        self.client = client

    async def execute(self, action: str, args: dict, source_agent: str) -> dict:
        """Dispatch action → result dict {ok, message, data?}."""
        if DRY_RUN:
            log.info("[DRY_RUN] action=%s args=%s", action, args)
            return {"ok": True, "message": f"[DRY_RUN] {action} not executed", "data": {}}
        method = getattr(self, f"do_{action}", None)
        if method is None:
            return {"ok": False, "message": f"executor missing for action={action}", "data": None}
        try:
            return await method(args, source_agent)
        except httpx.HTTPStatusError as e:
            return {"ok": False, "message": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "data": None}
        except Exception as e:
            log.exception("executor failed for %s", action)
            return {"ok": False, "message": f"Exception: {e}", "data": None}

    # ─ Read-only ─
    async def do_query_status(self, args: dict, source: str) -> dict:
        online = await self.client.fetch_online(within_seconds=300)
        all_agents = await self.client.list_all_agents()
        online_ids = {a["agent_id"] for a in online}
        lines = []
        for a in all_agents[:30]:
            aid = a.get("agent_id", "?")
            mark = "online" if aid in online_ids else "offline"
            task = a.get("current_task") or "-"
            lines.append(f"- {aid} [{mark}] task: {task[:80]}")
        if not lines:
            lines = ["(нет агентов в системе)"]
        summary = (
            f"Всего агентов: {len(all_agents)}, онлайн (5 мин): {len(online)}.\n"
            + "\n".join(lines)
        )
        return {"ok": True, "message": summary, "data": {"online": len(online), "total": len(all_agents)}}

    async def do_query_agent_state(self, args: dict, source: str) -> dict:
        agent_id = args["agent_id"]
        try:
            state = await self.client.get_agent_state(agent_id)
            return {"ok": True, "message": f"State {agent_id}: {json.dumps(state, ensure_ascii=False)[:1000]}", "data": state}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"ok": False, "message": f"Агент {agent_id} не найден.", "data": None}
            raise

    async def do_list_inbox(self, args: dict, source: str) -> dict:
        limit = min(max(int(args.get("limit", 10)), 1), 50)
        msgs = await self.client.fetch_inbox(since_minutes=60, limit=limit)
        if not msgs:
            return {"ok": True, "message": "Inbox пуст за последний час.", "data": []}
        lines = [f"- [{m.get('from')}]: {(m.get('text') or '')[:120]}" for m in msgs[:limit]]
        return {"ok": True, "message": "Последние сообщения:\n" + "\n".join(lines), "data": msgs}

    async def do_ping_agent(self, args: dict, source: str) -> dict:
        target = args["agent_id"]
        text = args.get("text") or "ping от orchestrator"
        await self.client.send_dm(target, f"[ping from orchestrator] {text}")
        return {"ok": True, "message": f"Пинг отправлен агенту {target}.", "data": None}

    # ─ Communications ─
    async def do_send_dm(self, args: dict, source: str) -> dict:
        target = args["to"]
        text = args["text"]
        await self.client.send_dm(target, text, context={"forwarded_by": "orchestrator", "original_source": source})
        return {"ok": True, "message": f"Сообщение передано агенту {target}.", "data": None}

    async def do_broadcast(self, args: dict, source: str) -> dict:
        targets = args["to"] if isinstance(args.get("to"), list) else []
        text = args["text"]
        if not targets:
            return {"ok": False, "message": "Список получателей пуст.", "data": None}
        delivered, failed = [], []
        for t in targets[:5]:  # safety cap on non-mass broadcast (>5 → mass_dm path)
            try:
                await self.client.send_dm(t, text, context={"broadcast_by": "orchestrator", "original_source": source})
                delivered.append(t)
            except Exception as e:
                failed.append(f"{t}: {e}")
        msg = f"Доставлено {len(delivered)}/{len(targets)}: {', '.join(delivered)}."
        if failed:
            msg += f"\nОшибки: {'; '.join(failed[:3])}"
        return {"ok": True, "message": msg, "data": {"delivered": delivered, "failed": failed}}

    async def do_room_post(self, args: dict, source: str) -> dict:
        room_key = args["room_key"]
        text = args["text"]
        # MCP tool room_post через /mcp/messages
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {
                "name": "room_post",
                "arguments": {"room_key": room_key, "text": text},
            },
        }
        r = await self.client._http.post("/mcp/messages", json=payload)
        if r.status_code >= 400:
            return {"ok": False, "message": f"room_post HTTP {r.status_code}: {r.text[:200]}", "data": None}
        return {"ok": True, "message": f"Опубликовано в комнате (key={room_key[:8]}...).", "data": r.json()}

    async def do_room_join(self, args: dict, source: str) -> dict:
        room_key = args["room_key"]
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": "room_join", "arguments": {"room_key": room_key}},
        }
        r = await self.client._http.post("/mcp/messages", json=payload)
        if r.status_code >= 400:
            return {"ok": False, "message": f"room_join HTTP {r.status_code}: {r.text[:200]}", "data": None}
        return {"ok": True, "message": f"Вступил в комнату (key={room_key[:8]}...).", "data": r.json()}

    async def do_room_read(self, args: dict, source: str) -> dict:
        room_key = args["room_key"]
        limit = min(max(int(args.get("limit", 20)), 1), 50)
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": "room_read", "arguments": {"room_key": room_key, "limit": limit}},
        }
        r = await self.client._http.post("/mcp/messages", json=payload)
        if r.status_code >= 400:
            return {"ok": False, "message": f"room_read HTTP {r.status_code}: {r.text[:200]}", "data": None}
        data = r.json()
        msgs = (data.get("result") or {}).get("messages") or data.get("messages") or []
        lines = [f"- [{m.get('agent_id','?')}]: {(m.get('text') or '')[:150]}" for m in msgs[:limit]]
        summary = f"Последние {len(lines)} сообщений в комнате:\n" + ("\n".join(lines) if lines else "(пусто)")
        return {"ok": True, "message": summary, "data": msgs}

    # ─ Memory ─
    async def do_remember_fact(self, args: dict, source: str) -> dict:
        domain = args["domain"]
        task = args["task"]
        result = args.get("result", "")
        await self.client.remember(domain=domain, task=task, result=result, feedback="positive")
        return {"ok": True, "message": f"Записано в domain={domain}.", "data": None}

    async def do_cognitive_recall(self, args: dict, source: str) -> dict:
        query = args["query"]
        domain = args.get("domain") or None
        top_k = min(max(int(args.get("top_k", 5)), 1), 20)
        mcp_args = {"query": query, "top_k": top_k}
        if domain:
            mcp_args["domain"] = domain
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": "cognitive_recall", "arguments": mcp_args},
        }
        # Semantic KNN дороже остальных tool calls — даём ему 60s (default httpx timeout
        # ~5-10s слишком мал для cold L3 search). Если совсем нет ответа — explicit error.
        try:
            r = await self.client._http.post("/mcp/messages", json=payload, timeout=60.0)
        except httpx.ReadTimeout:
            return {"ok": False, "message": f"recall timeout (>60s) для query '{query[:60]}' — попробуй сократить query или уменьшить top_k.", "data": None}
        if r.status_code >= 400:
            return {"ok": False, "message": f"recall HTTP {r.status_code}: {r.text[:200]}", "data": None}
        data = r.json()
        result = (data.get("result") or {})
        items = result.get("items") or result.get("results") or []
        if not items:
            return {"ok": True, "message": f"По запросу '{query[:60]}' ничего не найдено (domain={domain or 'any'}).", "data": []}
        lines = []
        for item in items[:top_k]:
            preview = json.dumps(item, ensure_ascii=False)[:250]
            lines.append(f"- {preview}")
        return {"ok": True, "message": f"Найдено {len(items)} (top {top_k}):\n" + "\n".join(lines), "data": items}

    async def do_analyze_media(self, args: dict, source: str) -> dict:
        """Получить результат media-анализа: transcript + URL'ы кадров.

        Делает recall по domain=media_analysis с фильтром по media_id.
        Возвращает summary человеческим текстом — для последующего room_post.
        """
        media_id = args["media_id"]
        mcp_args = {"query": f"media_id {media_id}", "domain": "media_analysis", "top_k": 5}
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": "cognitive_recall", "arguments": mcp_args},
        }
        try:
            r = await self.client._http.post("/mcp/messages", json=payload, timeout=60.0)
        except httpx.ReadTimeout:
            return {"ok": False, "message": f"recall timeout (>60s) для media {media_id}", "data": None}
        if r.status_code >= 400:
            return {"ok": False, "message": f"recall HTTP {r.status_code}: {r.text[:200]}", "data": None}
        data = r.json()
        result = data.get("result") or {}
        items = result.get("items") or result.get("results") or []
        # Find exact media_id match if possible
        matched = None
        for it in items:
            payload_str = json.dumps(it, ensure_ascii=False)
            if media_id in payload_str:
                matched = it
                break
        if not matched and items:
            matched = items[0]
        if not matched:
            return {"ok": False, "message": f"Media {media_id} не найден в L1 (возможно TTL 15 мин истёк или upload не прошёл).", "data": None}
        # Extract transcript + frames from payload
        p = matched.get("payload") or matched.get("raw_payload") or matched
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                pass
        transcript = p.get("transcript") or p.get("text") or ""
        frames = p.get("frames") or p.get("frame_urls") or []
        kind = p.get("kind") or "media"
        duration = p.get("duration_seconds") or p.get("duration") or "?"
        summary = f"Media {media_id} ({kind}, длительность {duration}s):\n\n"
        if transcript:
            summary += f"Транскрипт:\n{transcript[:1500]}\n\n"
        if frames:
            summary += f"Ключевых кадров: {len(frames)}\n"
            for i, f in enumerate(frames[:6]):
                url = f if isinstance(f, str) else f.get("url", "?")
                summary += f"  {i+1}. {url}\n"
        if not transcript and not frames:
            summary += "(анализ найден, но transcript/frames пустые)"
        return {"ok": True, "message": summary[:2500], "data": matched}

    # ─ Destructive ─
    async def do_delete_agent(self, args: dict, source: str) -> dict:
        agent_id = args["agent_id"]
        # Delete = revoke ВСЕ ключи агента; state остаётся как archive.
        # Используем revoke endpoint, но мы можем revoke только собственный ключ.
        # Поэтому для delete_agent делаем soft-marker: пишем event "agent_deleted"
        # в L1 + revoke ключ если он передан. Hard delete через SQL outside scope.
        msg = (
            f"Soft-delete для агента {agent_id}: помечен как deactivated в L1. "
            f"Hard-delete (revoke всех ключей + truncate state) требует ручного SQL."
        )
        try:
            await self.client.remember(
                domain="orchestrator_actions",
                task=f"delete_agent {agent_id}",
                result="soft_deleted by orchestrator",
                feedback="neutral",
            )
        except Exception as e:
            log.warning("remember failed: %s", e)
        return {"ok": True, "message": msg, "data": {"agent_id": agent_id, "mode": "soft"}}

    async def do_revoke_key(self, args: dict, source: str) -> dict:
        api_key = args["api_key"]
        # /agents/keys/revoke может revoke только key того же agent-а кто вызвал.
        # Orchestrator может revoke только свой собственный ключ → это бы парализовало его.
        # Поэтому мы возвращаем инструкцию owner-у через DM, а сами лишь логируем намерение.
        log.warning("revoke_key requested for key=%s... — manual ops required", api_key[:8])
        await self.client.remember(
            domain="orchestrator_actions",
            task=f"revoke_key request {api_key[:8]}...",
            result="logged; manual ops needed (orchestrator cannot revoke other agents keys via API)",
            feedback="neutral",
        )
        return {
            "ok": True,
            "message": (
                f"Запрос на revoke ключа {api_key[:8]}... залогирован. "
                "Hard-revoke требует ручной операции admin-ом (orchestrator не имеет прав revoke чужих ключей)."
            ),
            "data": {"api_key_prefix": api_key[:8], "mode": "logged_only"},
        }

    async def do_mass_dm(self, args: dict, source: str) -> dict:
        # После approval — рассылаем
        return await self.do_broadcast(args, source)

    async def do_purge_data(self, args: dict, source: str) -> dict:
        domain = args["domain"]
        days = int(args.get("older_than_days", 90))
        if days < 30:
            return {"ok": False, "message": "Защита: older_than_days < 30 не разрешён.", "data": None}
        # purge через SQL outside scope — логируем намерение
        await self.client.remember(
            domain="orchestrator_actions",
            task=f"purge_data domain={domain} older_than_days={days}",
            result="logged only; actual purge requires manual sql by admin",
            feedback="neutral",
        )
        return {
            "ok": True,
            "message": f"Запрос purge domain={domain} >{days}d залогирован. Hard-purge — manual.",
            "data": {"domain": domain, "older_than_days": days, "mode": "logged_only"},
        }

    # ─ Sentinel ─
    async def do_refuse(self, args: dict, source: str) -> dict:
        reason = args.get("reason", "(нет причины)")
        return {"ok": True, "message": f"Отказ: {reason}", "data": None}

    async def do_request_clarification(self, args: dict, source: str) -> dict:
        q = args.get("question", "Уточни запрос?")
        return {"ok": True, "message": q, "data": None}


# ─── Main loop ─────────────────────────────────────────────────────────────
class OrchestratorDaemon:
    def __init__(self, cfg: OrchestratorConfig):
        self.cfg = cfg
        self.client = CogClient(cfg)
        self.parser = DeepSeekParser(cfg)
        self.executor = Executor(cfg, self.client)
        self._stop = False

    async def close(self):
        await self.client.close()
        await self.parser.close()

    async def request_approval(self, source: str, parsed: dict, original_text: str) -> tuple[bool, str]:
        """Шлёт owner-у DM с запросом approval и ждёт reply YES/NO до timeout.

        Returns (approved, status_str).
        """
        if not self.cfg.owner_agent_id:
            return False, "no_owner_configured"

        approval_id = f"approval-{int(time.time() * 1000)}"
        action = parsed["action"]
        args = parsed["args"]
        msg = (
            f"APPROVAL REQUIRED [{approval_id}]\n\n"
            f"Запрос от: {source}\n"
            f"Действие: {action} (DESTRUCTIVE)\n"
            f"Параметры: {json.dumps(args, ensure_ascii=False)}\n"
            f"Причина: {parsed.get('reasoning', '-')}\n"
            f"Исходный текст: {original_text[:300]}\n\n"
            f"Ответь YES для approve или NO для cancel. Timeout: "
            f"{self.cfg.approval_timeout_seconds // 60} мин."
        )
        try:
            await self.client.send_dm(
                self.cfg.owner_agent_id,
                msg,
                context={"kind": "approval_request", "approval_id": approval_id, "action": action},
            )
        except Exception as e:
            log.error("approval request DM failed: %s", e)
            return False, f"dm_failed:{e}"

        # Persist pending
        pending = read_pending_approvals()
        pending.append({
            "approval_id": approval_id,
            "action": action,
            "args": args,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        write_pending_approvals(pending)

        # Wait loop — poll inbox for owner reply
        deadline = time.monotonic() + self.cfg.approval_timeout_seconds
        log.info("approval %s sent to %s, waiting %ds", approval_id, self.cfg.owner_agent_id, self.cfg.approval_timeout_seconds)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(10, max(2, self.cfg.poll_interval_seconds)))
            try:
                inbox = await self.client.fetch_inbox(since_minutes=int(self.cfg.approval_timeout_seconds / 60) + 2, limit=50)
            except Exception as e:
                log.debug("approval poll failed (transient): %s", e)
                continue
            for m in inbox:
                if m.get("from") != self.cfg.owner_agent_id:
                    continue
                text_lower = (m.get("text") or "").strip().lower()
                # Match either explicit YES/NO or "YES <approval_id>"
                if approval_id.lower() in text_lower:
                    if "yes" in text_lower:
                        self._clear_pending(approval_id)
                        return True, "approved"
                    if "no" in text_lower:
                        self._clear_pending(approval_id)
                        return False, "declined"
                else:
                    # Owner sometimes отвечает просто "yes" — берём последний reply
                    if text_lower == "yes" or text_lower.startswith("yes "):
                        # Confirm only если это самое свежее
                        self._clear_pending(approval_id)
                        return True, "approved_implicit"
                    if text_lower == "no" or text_lower.startswith("no "):
                        self._clear_pending(approval_id)
                        return False, "declined_implicit"

        # Timeout
        self._clear_pending(approval_id)
        log.warning("approval %s timed out", approval_id)
        return False, "timeout"

    def _clear_pending(self, approval_id: str) -> None:
        pending = read_pending_approvals()
        pending = [p for p in pending if p.get("approval_id") != approval_id]
        write_pending_approvals(pending)

    async def log_decision(self, payload: dict) -> None:
        if not self.cfg.log_decisions_to_l1:
            return
        try:
            task = f"action={payload['action']} source={payload['source_agent']}"
            result = json.dumps(payload, ensure_ascii=False)[:1900]
            await self.client.remember(
                domain="orchestrator_decisions",
                task=task,
                result=result,
                feedback="neutral",
            )
        except Exception as e:
            log.warning("L1 log failed: %s", e)

    async def handle_message(self, msg: dict) -> None:
        """Полная обработка одного DM от source."""
        source = msg.get("from") or "(unknown)"
        text = msg.get("text") or ""
        if not text.strip():
            return

        # Skip own/system messages and approval-replies (already handled in approval loop)
        if should_ignore_message(msg, orchestrator_id=self.cfg.orchestrator_id):
            log.debug("ignored msg from %s (self/system)", source)
            return

        # Skip if text == YES/NO and it's from owner — это approval-ответ, не команда
        tl = text.strip().lower()
        if source == self.cfg.owner_agent_id and tl in ("yes", "no") or tl.startswith("yes ") or tl.startswith("no "):
            log.debug("skipping approval-style reply from owner: %r", text[:60])
            return

        log.info("processing DM from %s: %s", source, text[:100])

        # Parse → either single action or multi-step plan
        parsed_raw = await self.parser.parse_raw(source, text)
        steps = extract_plan(parsed_raw)
        is_multi_step = len(steps) > 1
        log.info("plan has %d step(s): %s", len(steps), [s["action"] for s in steps])

        # Escalate broadcast→mass_dm в каждом step
        for s in steps:
            upgraded = expand_mass_dm_threshold(s["action"], s["args"])
            if upgraded != s["action"]:
                log.info("upgraded step action %s → %s", s["action"], upgraded)
                s["action"] = upgraded

        # Approval gate — для ВСЕЙ chain если есть destructive
        destructive_steps = plan_has_destructive(steps)
        requires_approval = bool(destructive_steps)
        approval_status = "not_required"

        if requires_approval:
            if source != self.cfg.owner_agent_id:
                refusal = (
                    f"План содержит destructive actions ({', '.join(destructive_steps)}) — "
                    f"только owner может их инициировать. Запрос отклонён."
                )
                await self._reply(source, refusal)
                await self.log_decision(decision_payload(
                    source_agent=source,
                    source_text=text,
                    parsed={"plan": steps, "destructive": destructive_steps},
                    requires_approval=True,
                    approval_status="declined_non_owner",
                    execution_result={"ok": False, "message": refusal},
                ))
                return
            # Single approval covers entire chain — show all destructive actions
            approval_summary = {
                "action": f"chain[{','.join(destructive_steps)}]" if is_multi_step else steps[0]["action"],
                "args": {"steps": [{"a": s["action"], "args": s["args"]} for s in steps]},
            }
            approved, status = await self.request_approval(source, approval_summary, text)
            approval_status = status
            if not approved:
                msg_txt = f"План НЕ выполнен (approval status: {status})."
                await self._reply(source, msg_txt)
                await self.log_decision(decision_payload(
                    source_agent=source,
                    source_text=text,
                    parsed={"plan": steps},
                    requires_approval=True,
                    approval_status=status,
                    execution_result={"ok": False, "message": msg_txt},
                ))
                return

        # Execute steps sequentially, stop on first failure
        step_results = []
        step_messages = []
        final_ok = True
        for idx, step in enumerate(steps):
            # Подставить STEP{N}_RESULT placeholders в args из предыдущих результатов
            substituted_args = substitute_step_placeholders(step["args"], step_messages)
            result = await self.executor.execute(step["action"], substituted_args, source)
            step_results.append(result)
            step_messages.append(result.get("message") or "")
            if not result.get("ok", False):
                log.warning("step %d (%s) failed: %s — stopping chain", idx+1, step["action"], result.get("message"))
                final_ok = False
                break

        # Compose reply — single result for 1-step, multi-block for chain
        if is_multi_step:
            reply_lines = []
            for i, (s, r) in enumerate(zip(steps[:len(step_results)], step_results), 1):
                mark = "✓" if r.get("ok") else "✗"
                msg = (r.get("message") or "(no message)")[:800]
                reply_lines.append(f"{mark} step {i} ({s['action']}): {msg}")
            if final_ok:
                reply_lines.insert(0, f"План из {len(step_results)} шагов выполнен:")
            else:
                reply_lines.insert(0, f"План прерван после {len(step_results)} из {len(steps)} шагов:")
            reply_text = "\n".join(reply_lines)
        else:
            reply_text = step_results[0].get("message", "(executed; no message)")

        prefix = "[orchestrator] "
        await self._reply(source, prefix + reply_text[:3500])

        # Log entire plan
        await self.log_decision(decision_payload(
            source_agent=source,
            source_text=text,
            parsed={"plan": steps, "multi_step": is_multi_step},
            requires_approval=requires_approval,
            approval_status=approval_status,
            execution_result={"ok": final_ok, "step_results": [{"action": s["action"], "ok": r.get("ok"), "message": (r.get("message") or "")[:200]} for s, r in zip(steps, step_results)]},
        ))

    async def _reply(self, to: str, text: str) -> None:
        if not to or to == self.cfg.orchestrator_id:
            return
        try:
            await self.client.send_dm(to, text, context={"from_orchestrator": True})
        except Exception as e:
            log.error("reply DM to %s failed: %s", to, e)

    async def poll_once(self) -> int:
        """Один цикл polling. Returns кол-во обработанных новых DM."""
        try:
            # since_minutes окно — берём с запасом для надёжности
            window = max(2, int(self.cfg.poll_interval_seconds / 60) + 2)
            msgs = await self.client.fetch_inbox(since_minutes=window, limit=50)
        except Exception as e:
            log.warning("fetch_inbox failed (transient): %s", e)
            return 0

        last_seen = read_last_processed_id()
        # messages are newest-first; reverse to process oldest-first
        new_msgs: list[dict] = []
        for m in reversed(msgs):
            mid = str(m.get("id") or "")
            if not mid:
                continue
            if mid == last_seen:
                # Сбрасываем накопленный лист — всё что выше уже обработано
                new_msgs.clear()
                continue
            new_msgs.append(m)

        # Если last_seen не встретился (например, после первого старта),
        # обрабатываем только последний DM каждые poll-cycle, чтобы не
        # ретроспективно реагировать на старые сообщения.
        if not last_seen and len(new_msgs) > 1:
            log.info("first run: skipping %d backlog messages, will react to new ones only", len(new_msgs) - 1)
            new_msgs = new_msgs[-1:]

        processed = 0
        for m in new_msgs:
            try:
                await self.handle_message(m)
            except Exception:
                log.exception("handle_message crashed for id=%s", m.get("id"))
            finally:
                write_last_processed_id(str(m.get("id") or ""))
                processed += 1
        return processed

    async def run_loop(self) -> None:
        log.info(
            "Cogcore Orchestrator daemon starting: api=%s id=%s owner=%s model=%s poll=%ds dry_run=%s",
            self.cfg.api_base, self.cfg.orchestrator_id, self.cfg.owner_agent_id or "(none)",
            self.cfg.deepseek_model, self.cfg.poll_interval_seconds, DRY_RUN,
        )
        # Initial heartbeat (mark online)
        await self.client.heartbeat(current_task="orchestrator boot")
        last_heartbeat = time.monotonic()
        while not self._stop:
            cycle_start = time.monotonic()
            try:
                count = await self.poll_once()
                if count:
                    log.info("processed %d new message(s)", count)
            except Exception:
                log.exception("poll_once crashed")
            # Heartbeat every 60s
            if time.monotonic() - last_heartbeat >= 60:
                await self.client.heartbeat(current_task="polling inbox")
                last_heartbeat = time.monotonic()
            # Sleep остаток интервала
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.5, self.cfg.poll_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)

    def request_stop(self) -> None:
        self._stop = True


async def main() -> int:
    cfg = load_config()
    daemon = OrchestratorDaemon(cfg)

    import signal

    def _handle_sigterm(sig, frame):
        log.info("received signal %s, requesting stop", sig)
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    try:
        await daemon.run_loop()
    finally:
        await daemon.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
