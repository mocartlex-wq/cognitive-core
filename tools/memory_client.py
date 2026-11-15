"""Durable stdlib memory delivery. Acknowledgement loss is safe with /events idempotency.

No background model calls, transcript scanning or credentials stored in the queue.
The caller supplies the current authenticated endpoint and key for each drain.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID


class MemoryOutbox:
    def __init__(self, path: str | Path, endpoint: str, api_key: str):
        self.path = Path(path)
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        # A changed account/key must not silently receive another account's queue.
        self.scope = hashlib.sha256((self.endpoint + "\n" + api_key).encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                scope TEXT NOT NULL, delivery_key TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT, record_id TEXT,
                created_at REAL NOT NULL, acknowledged_at REAL,
                PRIMARY KEY (scope, delivery_key))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, *, source_agent: str, domain: str, payload: dict, delivery_key: str,
                content_addressed: bool = False) -> str:
        body = json.dumps({"source_agent": source_agent, "domain": domain, "payload": payload,
                           "idempotency_key": delivery_key}, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries (scope, delivery_key, body, created_at) VALUES (?,?,?,?)",
                       (self.scope, delivery_key, body, time.time()))
            saved = db.execute("SELECT body FROM deliveries WHERE scope=? AND delivery_key=?",
                               (self.scope, delivery_key)).fetchone()
            if saved["body"] != body and not content_addressed:
                raise ValueError("Delivery key already queued with different content")
        return delivery_key

    def _send(self, body: str) -> str:
        req = urllib.request.Request(self.endpoint + "/events", data=body.encode("utf-8"),
                                     headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.load(response)
        if result.get("status") != "accepted":
            raise ValueError("Memory did not acknowledge acceptance")
        return str(UUID(str(result["id"])))

    def drain(self, *, limit: int = 10, force: bool = False) -> dict:
        with self.connect() as db:
            pending = db.execute(
                "SELECT delivery_key, body, attempts FROM deliveries WHERE scope=? AND status='pending' "
                "AND (next_attempt<=? OR ?) ORDER BY created_at, delivery_key LIMIT ?",
                (self.scope, time.time(), force, max(1, min(limit, 100))),
            ).fetchall()
        for row in pending:
            try:
                record_id = self._send(row["body"])
            except Exception as exc:
                code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
                permanent = code is not None and 400 <= code < 500 and code not in (408, 429)
                # Never log response bodies, request headers or exception URLs.
                error = f"HTTP {code}" if code else type(exc).__name__
                delay = min(300, 2 ** min(row["attempts"] + 1, 8))
                with self.connect() as db:
                    db.execute("UPDATE deliveries SET status=?, attempts=attempts+1, error=?, next_attempt=? "
                               "WHERE scope=? AND delivery_key=? AND status='pending'",
                               ("failed" if permanent else "pending", error, time.time() + delay,
                                self.scope, row["delivery_key"]))
            else:
                with self.connect() as db:
                    db.execute("UPDATE deliveries SET status='acked', attempts=attempts+1, error=NULL, "
                               "record_id=?, acknowledged_at=? WHERE scope=? AND delivery_key=?",
                               (record_id, time.time(), self.scope, row["delivery_key"]))
        return self.status()

    def status(self) -> dict:
        with self.connect() as db:
            counts = {r["status"]: r["n"] for r in db.execute(
                "SELECT status,count(*) AS n FROM deliveries WHERE scope=? GROUP BY status", (self.scope,))}
            failures = [dict(r) for r in db.execute(
                "SELECT delivery_key,status,attempts,error FROM deliveries WHERE scope=? AND status!='acked' "
                "ORDER BY created_at LIMIT 20", (self.scope,))]
        return {"pending": counts.get("pending", 0), "acked": counts.get("acked", 0),
                "failed": counts.get("failed", 0), "unacknowledged": failures}

    def receipt(self, delivery_key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT status,record_id,error FROM deliveries WHERE scope=? AND delivery_key=?",
                             (self.scope, delivery_key)).fetchone()
        return dict(row) if row else None
