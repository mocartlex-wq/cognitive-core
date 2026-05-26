import asyncio
from datetime import datetime, timezone

from app.config import settings
from app.db.postgres import get_pool
from app.security.audit import log_audit
from app.services.consolidator import (
    daily_consolidate,
    run_monthly_audit,
    weekly_consolidate,
)


async def run_daily_cycle():
    """Ежедневная консолидация L1→L2."""
    try:
        result = await daily_consolidate()
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
        domains = [r["domain"] for r in rows]

    results = []
    for domain in domains:
        try:
            result = await weekly_consolidate(domain)
            results.append({"domain": domain, "result": result})
        except Exception as e:
            results.append({"domain": domain, "error": str(e)})

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
        domains = [r["domain"] for r in rows]

    results = []
    for domain in domains:
        try:
            result = await run_monthly_audit(domain)
            results.append({"domain": domain, "result": result})
        except Exception as e:
            results.append({"domain": domain, "error": str(e)})

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
