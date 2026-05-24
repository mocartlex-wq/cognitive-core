"""Outbox publisher — читает replication_outbox и публикует в NATS JetStream.

Принципы:
  - Publisher запускается как asyncio task в lifespan FastAPI.
  - Раз в N секунд (poll_interval, default 1s) делает SELECT unprocessed FOR UPDATE SKIP LOCKED.
  - Публикует в JetStream subject `cognitive.repl.<kind>` с payload как JSON.
  - При успехе: UPDATE published_at = NOW().
  - При ошибке: UPDATE last_error, publish_attempts++ — через retry_after поднимется.

Idempotency: каждое событие имеет UUID event_id (UNIQUE в outbox + используется
consumer'ом на local-стороне для ON CONFLICT DO NOTHING).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger("cognitive.replication")

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
STREAM_NAME = os.environ.get("REPLICATION_STREAM", "COGNITIVE_REPL")
SUBJECT_PREFIX = "cognitive.repl"
POLL_INTERVAL = float(os.environ.get("REPLICATION_POLL_SEC", "1.0"))
BATCH_SIZE = int(os.environ.get("REPLICATION_BATCH", "50"))


async def write_outbox_event(
    conn: asyncpg.Connection,
    kind: str,
    payload: dict[str, Any],
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Атомарная запись в replication_outbox.

    Должно вызываться в той же транзакции что и основной write
    (например — SET LOCAL synchronous_commit = on внутри tx).

    Args:
        conn: открытое соединение asyncpg (или транзакция)
        kind: l1_event | l3_knowledge | l3_tool | l4_snapshot | agent_state | l5_audit
        payload: словарь с состоянием объекта для repro у consumer'а
        event_id: опционально, иначе генерируется UUID v4

    Returns:
        event_id записанного события
    """
    if event_id is None:
        event_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO replication_outbox (event_id, kind, payload)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id,
        kind,
        json.dumps(payload, default=str, ensure_ascii=False),
    )
    return event_id


class OutboxPublisher:
    """Asyncio-task что периодически публикует unprocessed outbox в NATS."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db = db_pool
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._nats: Any = None
        self._js: Any = None
        self._stream_ensured = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-publisher")
        logger.info("OutboxPublisher started, poll=%ss batch=%s", POLL_INTERVAL, BATCH_SIZE)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._nats:
            try:
                await self._nats.close()
            except Exception:
                pass

    async def _ensure_nats(self) -> bool:
        if self._js is not None:
            return True
        try:
            import nats  # nats-py
        except ImportError:
            logger.warning("nats-py not installed — replication disabled")
            return False
        try:
            self._nats = await nats.connect(
                NATS_URL,
                connect_timeout=5,
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
            )
            self._js = self._nats.jetstream()
            await self._ensure_stream()
            logger.info("Connected to NATS at %s", NATS_URL)
            return True
        except Exception as e:
            logger.warning("NATS connect failed: %s — will retry", e)
            return False

    async def _ensure_stream(self) -> None:
        if self._stream_ensured:
            return
        from nats.js.api import StreamConfig, RetentionPolicy, StorageType
        cfg = StreamConfig(
            name=STREAM_NAME,
            subjects=[f"{SUBJECT_PREFIX}.>"],
            retention=RetentionPolicy.LIMITS,
            max_age=60 * 60 * 24 * 30 * 1_000_000_000,  # 30 days in ns
            max_bytes=10 * 1024 * 1024 * 1024,  # 10 GB
            storage=StorageType.FILE,
            duplicate_window=60 * 60 * 1_000_000_000,  # 1h dedup
        )
        try:
            await self._js.add_stream(cfg)
            logger.info("Created NATS stream %s", STREAM_NAME)
        except Exception:
            # уже существует — обновим/проигнорируем
            try:
                await self._js.update_stream(cfg)
            except Exception:
                pass
        self._stream_ensured = True

    async def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                if not await self._ensure_nats():
                    # Wait for stop event OR backoff timeout; both are normal exits.
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=min(backoff, 30))
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 1
                published = await self._tick()
                # Если есть ещё необработанные — крутимся быстрее
                wait = 0.1 if published >= BATCH_SIZE else POLL_INTERVAL
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("OutboxPublisher tick error: %s", e)
                await asyncio.sleep(2)

    async def _tick(self) -> int:
        """Один проход: достать batch, опубликовать, пометить published."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_id, kind, payload, publish_attempts
                FROM replication_outbox
                WHERE published_at IS NULL
                ORDER BY id
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                BATCH_SIZE,
            )
            if not rows:
                return 0
            ok_ids: list[int] = []
            err_ids: list[tuple[int, str]] = []
            for row in rows:
                subject = f"{SUBJECT_PREFIX}.{row['kind']}"
                # asyncpg возвращает JSONB как str — парсим обратно чтобы
                # не было double-encoding при сериализации в NATS-сообщение
                raw_payload = row["payload"]
                if isinstance(raw_payload, str):
                    try:
                        raw_payload = json.loads(raw_payload)
                    except Exception:
                        pass
                msg = json.dumps({
                    "event_id": str(row["event_id"]),
                    "kind": row["kind"],
                    "payload": raw_payload,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }, default=str, ensure_ascii=False).encode("utf-8")
                try:
                    await self._js.publish(
                        subject,
                        msg,
                        headers={"Nats-Msg-Id": str(row["event_id"])},  # для dedup
                    )
                    ok_ids.append(row["id"])
                except Exception as e:
                    err_ids.append((row["id"], str(e)[:200]))
                    logger.warning("publish failed for id=%s: %s", row["id"], e)
            # mark published
            if ok_ids:
                await conn.execute(
                    """
                    UPDATE replication_outbox
                    SET published_at = NOW(), last_error = NULL
                    WHERE id = ANY($1::bigint[])
                    """,
                    ok_ids,
                )
            for eid, err in err_ids:
                await conn.execute(
                    """
                    UPDATE replication_outbox
                    SET publish_attempts = publish_attempts + 1, last_error = $2
                    WHERE id = $1
                    """,
                    eid,
                    err,
                )
            if ok_ids:
                logger.info("published %s events to NATS", len(ok_ids))
            return len(ok_ids)
