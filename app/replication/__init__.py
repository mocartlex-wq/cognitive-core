"""Replication module — server→local mirror via NATS JetStream + Outbox pattern.

DS-architecture (chosen pattern: Hybrid):
  1. На server: каждое write в L1/L3/L4/agent_states атомарно с записью в
     replication_outbox таблицу.
  2. Outbox Publisher (asyncio task в lifespan FastAPI) читает unprocessed строки
     и публикует в NATS subjects: cognitive.repl.<kind>.
  3. Consumer на local подписан на эти subjects, применяет события идемпотентно
     к local Postgres через UNIQUE event_id + ON CONFLICT DO NOTHING.

Failure modes покрыты:
  - NATS down: publisher retry с backoff, outbox строки остаются (publish_attempts++)
  - Consumer down: durable consumer пересохраняет cursor в JetStream
  - Network split: при восстановлении NATS replay'ит messages с last-ack
"""
from .outbox import OutboxPublisher, write_outbox_event

__all__ = ["write_outbox_event", "OutboxPublisher"]
