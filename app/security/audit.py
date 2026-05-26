import json
from uuid import UUID

from app.db.postgres import get_pool


async def log_audit(
    agent_id: str,
    action: str,
    target_table: str = "",
    target_id: UUID | None = None,
    details: dict | None = None,
    ip_address: str = "",
    success: bool = True,
) -> None:
    """Записывает событие в l5_audit_log."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO l5_audit_log
                (agent_id, action, target_table, target_id, details, ip_address, success)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            agent_id,
            action,
            target_table,
            target_id,
            json.dumps(details or {}, ensure_ascii=False),
            ip_address,
            success,
        )
