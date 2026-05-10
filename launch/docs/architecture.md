# Architecture

## At a glance

```
                   ┌────────────────────────────────────────────┐
                   │ Browser • CLI • Claude Code • ChatGPT • …  │
                   └─────────────────────┬──────────────────────┘
                                         │  HTTPS
                          ┌──────────────▼─────────────┐
                          │     nginx  (edge profile)   │
                          │  TLS · CORS · rate limit    │
                          └──┬───────┬──────────┬───────┘
                             │       │          │
              ┌──────────────▼─┐  ┌──▼─────┐  ┌─▼─────────┐
              │  api  :8000    │  │ rooms  │  │ mcp :8765 │
              │  FastAPI       │  │ :9098  │  │ SSE       │
              │  L1–L5 memory  │  │ HTTP   │  │           │
              │  curator       │  │ long-  │  │           │
              │  embeddings    │  │ poll   │  │           │
              └──┬─────────────┘  └─┬──────┘  └────┬──────┘
                 │                  │              │
                 │   ┌──────────────┘              │
                 │   │                             │
        ┌────────▼───▼─────┐    ┌──────────────────▼────┐
        │   postgres :5432  │    │  redis  (-stack)       │
        │   pgvector +     │    │  AOF persistence       │
        │   pgcrypto       │    │  RediSearch index      │
        │   ─ l1_raw       │    │                        │
        │   ─ l2_daily     │    └────────────────────────┘
        │   ─ l3_master    │
        │   ─ rooms        │    ┌───────────────────────┐
        │   ─ NOTIFY       │───►│  pg-to-nats           │
        └──────────────────┘    │  LISTEN room_event    │
                                │  publish nats://      │
                                └────────────┬──────────┘
                                             │
                              ┌──────────────▼────────────┐
                              │    nats  :4222 / :8443ws  │
                              │    JetStream + WebSocket  │
                              └──────────────┬────────────┘
                                             │
                                             ▼
                                  ┌──────────────────┐
                                  │ wake-poller /    │
                                  │ wake-daemon-nats │
                                  │  (on agent boxes)│
                                  └──────────────────┘

        ┌──────────────────┐
        │  minio :9000     │  L4 snapshots (rolling, S3-compatible)
        └──────────────────┘
```

## Layers in 90 seconds

| Layer | Stored where | TTL | Lifecycle |
|-------|--------------|-----|-----------|
| **L1** raw events | postgres `l1_raw_events` | 14 d | Append-only. SHA-256 dedup trigger. Pruned daily at 03:00 UTC. |
| **L2** daily buffers | postgres `l2_daily_buffers` | indefinite | DeepSeek summarizes L1 per agent per day at 04:00 UTC. |
| **L3** master knowledge | postgres `l3_*` (knowledge / tools / entities / links) | indefinite | Promoted from L2 when repeated ≥2 days × confidence ≥0.6. KG extraction Sun 04:00. |
| **L4** snapshots | minio `l4-snapshots` | indefinite | Full snapshot every 4 weeks or 5% delta. tar.gz of L3 + agent state. |
| **L5** audit log | postgres `l5_audit_log` + `/var/log/cognitive-audit.jsonl` | indefinite | Tamper-evident. Privileged ops: lock acquire, write-tool exec, approval. |

## Push pipeline (sub-second)

The "long-poll feels real-time" trick:

1. `POST /rooms/{id}/answer/{qid}` writes a row to `room_messages`.
2. `AFTER INSERT` trigger fires `pg_notify('room_event', ...)` with the row payload.
3. `cognitive_pg_to_nats` (a 200-line Python service) holds a `LISTEN room_event` connection and republishes each notification on NATS subject `room.<id>.events`.
4. Anyone subscribed to that subject (the long-poll handler in rooms, or `cogcore-wake-daemon-nats.py` on a sleeping laptop) is woken in <1 s.
5. The asker's HTTP request returns with the freshly-stored answer.

Same pipeline carries `agent_inbox` events for direct messages.

## B+D orchestrator (offline fallback)

`/ask` flow when target may be offline:

```
Alice ─── POST /ask wait_for=[bob], timeout=25 ─────► rooms
                                                       │
                                                       ▼
                                             check bob.last_seen_at
                                                       │
                                ┌──────────────────────┴────────┐
                          online (<90s)              offline (>90s)
                                │                              │
                            wait up to                    wait 5s for
                            timeout for                   real bob, then
                            real /answer                  call DeepSeek
                                │                              │
                                ▼                              ▼
                        return real answer            generate proxy answer
                                                      tag [proxy-tentative]
                                                      store as bob-proxy
                                                              │
                                                              ▼
                                                      return to Alice
                                                              │
                                                      ────────┴─────────
                                                              │
                                          (later)             ▼
                              Bob wakes ─── GET /sync-pending?agent=bob
                                                              │
                                                              ▼
                                                  see question + proxy answer
                                                              │
                                                              ▼
                                                  POST /answer to override
```

Tunable via `PROXY_FALLBACK_AFTER` env (default 5 s) and `last_seen_at` threshold (90 s, hardcoded).

## What's NOT in the diagram

- **agent-runtime** (server-side persona inbox processor) — runs on host as systemd, not in compose. Optional.
- **conv-ui / approval / lock-mgr / kg / memory-exporter** — production-only deep-dive services. Not in the public bundle.
- **GPU embedding worker** — see `docs/LOCAL_LLM.md`.

## Footprint (idle, no users)

Measured on i5-7500 / 32 GB DDR4 / Linux 6.8:

| Container     | RAM     | CPU |
|---------------|---------|-----|
| postgres      | ~120 MB | <1% |
| redis-stack   | ~80 MB  | <1% |
| minio         | ~150 MB | <1% |
| nats          | ~30 MB  | <1% |
| api           | ~250 MB | <1% |
| mcp           | ~120 MB | <1% |
| rooms         | ~60 MB  | <1% |
| pg-to-nats    | ~40 MB  | <1% |
| **total**     | **~850 MB** | **<3%** |

With nginx edge profile add ~10 MB.
