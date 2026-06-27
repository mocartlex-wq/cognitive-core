import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db.postgres import get_pool
from app.db.redis import get_redis
from app.security.audit import log_audit
from app.services.consolidator import (
    daily_consolidate,
    run_monthly_audit,
    weekly_consolidate,
)

log = logging.getLogger(__name__)

# Persistent retry-queue для упавших доменов. Раньше: домен падает на LLM
# timeout → попадает в results.error → молча скипается на следующем цикле
# до тех пор пока в нём не будет новых событий (а это может никогда не
# случиться, если домен и так был «тихий»). Теперь: имя домена кладётся
# в Redis SET, на следующем цикле он явно включается в обход (даже если
# в основной выборке его нет), при успехе — выгребается из set'а.
RETRY_KEY_DAILY = "worker:retry:daily"
RETRY_KEY_WEEKLY = "worker:retry:weekly"
RETRY_KEY_MONTHLY = "worker:retry:monthly"
RETRY_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 дней — больше реальной паузы циклов


async def _retry_queue_pending(key: str) -> set[str]:
    try:
        r = await get_redis()
        members = await r.smembers(key)
        return set(members or [])
    except Exception as e:
        log.warning("retry-queue read failed key=%s err=%s", key, e)
        return set()


async def _retry_queue_add(key: str, domain: str) -> None:
    try:
        r = await get_redis()
        await r.sadd(key, domain)
        await r.expire(key, RETRY_TTL_SECONDS)
    except Exception as e:
        log.warning("retry-queue add failed key=%s domain=%s err=%s", key, domain, e)


async def _retry_queue_remove(key: str, domain: str) -> None:
    try:
        r = await get_redis()
        await r.srem(key, domain)
    except Exception as e:
        log.warning("retry-queue remove failed key=%s domain=%s err=%s", key, domain, e)


async def run_daily_cycle():
    """Ежедневная консолидация L1→L2.

    Сначала прогоняем retry-queue (домены с прошлого сбоя), потом основной
    daily_consolidate без domain-фильтра. Per-domain failures из обоих
    кладутся обратно в retry-queue, успешные — выгребаются."""
    # Сначала ретраим то, что висело с прошлого раза.
    retry_pending = await _retry_queue_pending(RETRY_KEY_DAILY)
    if retry_pending:
        log.info("daily retry-queue domains=%s", sorted(retry_pending))
        for retry_dom in sorted(retry_pending):
            try:
                r = await daily_consolidate(domain=retry_dom)
                # Если домен ушёл в результаты как «error» — оставляем в очереди.
                domain_results = r.get("results") or []
                failed = any(
                    d.get("status") == "error" and d.get("domain") == retry_dom
                    for d in domain_results
                )
                if not failed:
                    await _retry_queue_remove(RETRY_KEY_DAILY, retry_dom)
            except Exception as e:
                log.warning("daily retry failed domain=%s err=%s", retry_dom, e)

    try:
        result = await daily_consolidate()
        # Любой домен, упавший в основном проходе → ставим в retry-queue.
        for d in (result.get("results") or []):
            if d.get("status") == "error":
                dom = d.get("domain")
                if dom:
                    await _retry_queue_add(RETRY_KEY_DAILY, dom)
        await log_audit(
            agent_id="system",
            action="daily_consolidate",
            target_table="l2_daily_buffers",
            details=result,
            success=True,
        )
        return result
    except Exception as e:
        await log_audit(
            agent_id="system",
            action="daily_consolidate",
            details={"error": str(e)},
            success=False,
        )
        return {"status": "error", "detail": str(e)}


async def run_weekly_cycle():
    """Еженедельная консолидация L2→L3."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT domain FROM l2_daily_buffers
            WHERE date >= CURRENT_DATE - $1::int
            UNION
            SELECT DISTINCT domain FROM l3_master_knowledge WHERE effective_to IS NULL
        """, settings.weekly_days)
        domains = {r["domain"] for r in rows}

    # Подмешиваем доменов из retry-очереди прошлой попытки.
    retry_pending = await _retry_queue_pending(RETRY_KEY_WEEKLY)
    if retry_pending:
        log.info("weekly retry-queue domains=%s", sorted(retry_pending))
        domains |= retry_pending

    results = []
    for domain in sorted(domains):
        try:
            result = await weekly_consolidate(domain)
            results.append({"domain": domain, "result": result})
            if domain in retry_pending:
                await _retry_queue_remove(RETRY_KEY_WEEKLY, domain)
        except Exception as e:
            results.append({"domain": domain, "error": str(e)})
            await _retry_queue_add(RETRY_KEY_WEEKLY, domain)

    await log_audit(
        agent_id="system",
        action="weekly_consolidate",
        target_table="l3_master_knowledge",
        details={"results": results},
        success=True,
    )
    return results


async def run_monthly_cycle():
    """Ежемесячная ревизия L3."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT domain FROM l3_master_knowledge
            UNION
            SELECT DISTINCT domain FROM l2_daily_buffers
        """)
        domains = {r["domain"] for r in rows}

    retry_pending = await _retry_queue_pending(RETRY_KEY_MONTHLY)
    if retry_pending:
        log.info("monthly retry-queue domains=%s", sorted(retry_pending))
        domains |= retry_pending

    results = []
    for domain in sorted(domains):
        try:
            result = await run_monthly_audit(domain)
            results.append({"domain": domain, "result": result})
            if domain in retry_pending:
                await _retry_queue_remove(RETRY_KEY_MONTHLY, domain)
        except Exception as e:
            results.append({"domain": domain, "error": str(e)})
            await _retry_queue_add(RETRY_KEY_MONTHLY, domain)

    await log_audit(
        agent_id="system",
        action="monthly_audit",
        target_table="l3_master_knowledge",
        details={"results": results},
        success=True,
    )
    return results


async def run_error_digest():
    """Раз в 6 часов — собрать ошибки фронта и отправить дайджест на owner email
    (если ошибки были И owner_bootstrap_email задан в .env)."""
    try:
        from app.api.errors import send_digest_email_if_needed
        result = await send_digest_email_if_needed(hours=6)
        await log_audit(
            agent_id="system",
            action="frontend_errors_digest",
            target_table="l1_raw_events",
            details=result,
            success=True,
        )
        return result
    except Exception as e:
        await log_audit(
            agent_id="system",
            action="frontend_errors_digest",
            target_table="l1_raw_events",
            details={"error": str(e)[:300]},
            success=False,
        )
        return {"sent": False, "error": str(e)}


async def scheduler_loop():
    """Фоновый планировщик циклов."""
    last_daily = None
    last_weekly = None
    last_monthly = None
    last_digest_slot: int | None = None  # 0..3 (каждый 6-час слот суток)

    while True:
        now = datetime.now(timezone.utc)
        today = now.date()

        # Ежедневно в 2:00 UTC
        if last_daily != today and now.hour >= 2:
            await run_daily_cycle()
            last_daily = today

        # Еженедельно в понедельник 3:00 UTC
        if now.weekday() == 0 and last_weekly != today and now.hour >= 3:
            await run_weekly_cycle()
            last_weekly = today

        # Ежемесячно 1-го числа в 4:00 UTC
        if now.day == 1 and last_monthly != today and now.hour >= 4:
            await run_monthly_cycle()
            last_monthly = today

        # Каждые 6 часов: email-дайджест фронт-ошибок (00, 06, 12, 18 UTC)
        # Slot = (date * 4) + (hour // 6) — уникально на сутки
        slot = today.toordinal() * 4 + now.hour // 6
        if last_digest_slot != slot and now.hour % 6 == 0:
            await run_error_digest()
            last_digest_slot = slot

        await asyncio.sleep(3600)  # Проверка каждый час
