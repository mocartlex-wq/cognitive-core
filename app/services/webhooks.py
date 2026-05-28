"""Outbound webhook delivery + per-tenant event notifications.

Owner-mandate (M4 v1.0 roadmap): owner/tenants хотят получать уведомления о
key-events (новый агент claimed, billing payment, room created, quota exceeded,
agent offline) в Slack / Discord / Telegram / любой generic endpoint.

Дизайн:
  send_webhook(url, event_type, payload, secret=None)
    - POST JSON. Если secret задан → header X-Cogcore-Signature = HMAC-SHA256
      от raw body (hex). Receiver сверяет → доверяет источнику.
    - Timeout 6s. 1 retry на 5xx (transient). Fire-and-forget: НИКОГДА не
      поднимает наружу — webhook down НЕ ломает основной flow, только лог.
    - Provider auto-detect по URL: Slack → {text}, Discord → {content},
      generic → raw event-envelope JSON.

  notify_event(owner_user_id, event_type, data)
    - Lookup user_webhooks для owner-а, шлёт на каждый enabled+subscribed.
    - Graceful: если таблицы ещё нет (миграция не накатана) — тихо no-op.
    - Decrypt secret через app.security.secrets_vault (Fernet).

Security:
  - HMAC подпись даёт receiver-у proof-of-origin (anti-spoof).
  - НЕ логируем secret / signature / decrypted значения.
  - URL-валидация (anti-SSRF) живёт в app/api/webhooks.py на write-path;
    здесь дополнительно НЕ резолвим/не follow-редиректим (follow_redirects=False)
    чтобы 30x не увёл POST на internal target.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Канонический реестр событий. Ключ = event_type в payload-envelope,
# значение = человекочитаемый label (для UI чекбоксов + лог).
WEBHOOK_EVENTS: dict[str, str] = {
    "agent.claimed": "Новый агент подключён (claimed)",
    "billing.payment": "Платёж / изменение подписки",
    "room.created": "Создана комната (room)",
    "quota.exceeded": "Превышена квота",
    "agent.offline": "Агент ушёл offline",
}

_TIMEOUT_S = 6.0
_SIGNATURE_HEADER = "X-Cogcore-Signature"
_EVENT_HEADER = "X-Cogcore-Event"


# ─────────────────────────────────────────────────────────────────────────
# Provider payload formatting
# ─────────────────────────────────────────────────────────────────────────
def _detect_provider(url: str) -> str:
    """Грубый detect по hostname/path. Возвращает 'slack'|'discord'|'generic'."""
    u = (url or "").lower()
    if "hooks.slack.com" in u:
        return "slack"
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    return "generic"


def _summarize(event_type: str, payload: dict[str, Any]) -> str:
    """Человекочитаемая строка для Slack/Discord text-форматов."""
    label = WEBHOOK_EVENTS.get(event_type, event_type)
    # Берём пару «говорящих» полей если есть, не вываливая весь payload.
    bits = []
    for k in ("agent_id", "tier", "room_id", "quota", "amount", "reason"):
        if k in payload and payload[k] not in (None, ""):
            bits.append(f"{k}={payload[k]}")
    suffix = (" — " + ", ".join(bits)) if bits else ""
    return f"[Cognitive Core] {label}{suffix}"


def _format_body(provider: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Сформировать provider-specific JSON-тело.

    generic — полный envelope (event/data/ts), receiver парсит сам.
    slack   — {"text": "..."} (плюс envelope в attachments-less виде).
    discord — {"content": "..."}.
    """
    envelope = {
        "event": event_type,
        "data": payload,
        "source": "cognitive-core",
    }
    if provider == "slack":
        return {"text": _summarize(event_type, payload), "event": event_type}
    if provider == "discord":
        return {"content": _summarize(event_type, payload)}
    return envelope


def _sign(body_bytes: bytes, secret: str) -> str:
    """HMAC-SHA256(secret, body) → hex. Receiver сверяет тем же алгоритмом."""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# Core sender (fire-and-forget)
# ─────────────────────────────────────────────────────────────────────────
async def send_webhook(
    url: str,
    event_type: str,
    payload: dict[str, Any],
    secret: Optional[str] = None,
) -> dict[str, Any]:
    """POST webhook. Возвращает {ok, status} — НИКОГДА не raise.

    - 1 retry на 5xx / network error (transient).
    - follow_redirects=False — 30x не должен увести POST на другой хост
      (anti-SSRF defense-in-depth поверх URL-валидации на write-path).
    """
    provider = _detect_provider(url)
    body = _format_body(provider, event_type, payload)
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "cognitive-core-webhooks/1.0",
        _EVENT_HEADER: event_type,
    }
    if secret:
        headers[_SIGNATURE_HEADER] = "sha256=" + _sign(body_bytes, secret)

    last_status = 0
    for attempt in (1, 2):  # initial + 1 retry
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)
            last_status = resp.status_code
            if resp.status_code < 400:
                logger.info(
                    "webhook: delivered event=%s provider=%s status=%s attempt=%s",
                    event_type, provider, resp.status_code, attempt,
                )
                return {"ok": True, "status": resp.status_code}
            if 500 <= resp.status_code < 600 and attempt == 1:
                logger.warning(
                    "webhook: 5xx event=%s status=%s — retrying",
                    event_type, resp.status_code,
                )
                continue
            # 4xx (или 5xx на втором attempt) — не ретраим дальше.
            logger.warning(
                "webhook: non-2xx event=%s provider=%s status=%s (no further retry)",
                event_type, provider, resp.status_code,
            )
            return {"ok": False, "status": resp.status_code}
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning(
                "webhook: transport error event=%s attempt=%s (%s)",
                event_type, attempt, type(e).__name__,
            )
            if attempt == 1:
                continue
            return {"ok": False, "status": last_status, "error": type(e).__name__}
        except Exception as e:  # noqa: BLE001 — fire-and-forget, никогда не падаем
            logger.warning(
                "webhook: unexpected error event=%s (%s)",
                event_type, type(e).__name__,
            )
            return {"ok": False, "status": last_status, "error": type(e).__name__}
    return {"ok": False, "status": last_status}


# ─────────────────────────────────────────────────────────────────────────
# Event fan-out: lookup tenant config + send
# ─────────────────────────────────────────────────────────────────────────
async def notify_event(
    owner_user_id: str,
    event_type: str,
    data: dict[str, Any],
) -> int:
    """Разослать event на все enabled+subscribed webhooks owner-а.

    Возвращает число endpoint-ов которым попытались доставить.
    Graceful: таблица отсутствует / DB-ошибка → 0, без raise (не ломаем flow).
    """
    if event_type not in WEBHOOK_EVENTS:
        logger.debug("webhook: unknown event_type=%s — skipping fan-out", event_type)
        return 0

    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id::text, url, events, secret_encrypted
                     FROM user_webhooks
                    WHERE owner_user_id = $1::uuid AND enabled = TRUE""",
                owner_user_id,
            )
    except Exception as e:  # noqa: BLE001 — таблицы может не быть / DB down
        logger.debug("webhook: notify_event lookup skipped (%s)", type(e).__name__)
        return 0

    sent = 0
    for row in rows:
        subscribed = row["events"] or []
        if event_type not in subscribed:
            continue
        secret = _decrypt_secret(row["secret_encrypted"])
        result = await send_webhook(row["url"], event_type, data, secret=secret)
        sent += 1
        # best-effort обновление last_triggered_at / last_status (не критично).
        await _update_delivery_status(pool, row["id"], result)
    return sent


def _decrypt_secret(secret_encrypted: Any) -> Optional[str]:
    """Расшифровать secret из BYTEA. None если пусто / ошибка (graceful)."""
    if not secret_encrypted:
        return None
    try:
        from app.security.secrets_vault import decrypt
        return decrypt(secret_encrypted)
    except Exception:  # noqa: BLE001 — не светим crypto-детали, шлём без подписи
        logger.warning("webhook: secret decrypt failed — sending unsigned")
        return None


async def _update_delivery_status(pool: Any, webhook_id: str, result: dict[str, Any]) -> None:
    """Записать last_triggered_at + last_status. Best-effort, не raise."""
    status = "ok" if result.get("ok") else f"fail:{result.get('status') or result.get('error', '?')}"
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE user_webhooks
                      SET last_triggered_at = NOW(), last_status = $2
                    WHERE id = $1::uuid""",
                webhook_id, status[:64],
            )
    except Exception:  # noqa: BLE001
        pass
