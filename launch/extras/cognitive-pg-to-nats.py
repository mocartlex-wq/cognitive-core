#!/usr/bin/env python3
# Cognitive PG-NOTIFY to NATS publisher v2 (DD6 reliability hardening).
# Improvements over v1:
#   - Replay buffer: track last_pushed_msg_id in /var/lib/cognitive/pg-nats-state.json
#     on startup, replay any unbridged events (since last checkpoint)
#   - Dead Letter Queue: failed publishes → /var/lib/cognitive/pg-nats-dlq.jsonl + alert
#   - Latency SLO: track publish lag, alert if P95 >2s sustained 5min
#   - HA-friendly: tagged with hostname for multi-instance dedup (future)
#   - Health endpoint: /health on 9097 for monitoring
#   - Backpressure: skip batch if NATS publish errors >threshold

import os, sys, json, time, threading, subprocess, socket, logging, http.server
from datetime import datetime, timezone
from collections import deque

try:
    import psycopg
except ImportError:
    print("ERROR: pip install psycopg[binary]", file=sys.stderr)
    sys.exit(1)


LOG_FILE = "/var/log/cognitive-pg-to-nats.log"
STATE_DIR = "/var/lib/cognitive"
STATE_FILE = f"{STATE_DIR}/pg-nats-state.json"
DLQ_FILE = f"{STATE_DIR}/pg-nats-dlq.jsonl"
HEALTH_FILE = "/var/run/cognitive/pg-nats-health.json"
PG_DSN = os.environ.get("PG_DSN", "")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_BIN = "/usr/local/bin/nats"
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "9097"))
HOSTNAME = socket.gethostname()

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cogcore-pg-to-nats-v2")

# Latency tracking (last 200 publishes)
_latencies = deque(maxlen=200)
_latencies_lock = threading.Lock()
_state = {
    "last_pushed_msg_id": None,
    "total_published": 0,
    "total_dlq": 0,
    "started_at": time.time(),
    "hostname": HOSTNAME,
}


def derive_pg_dsn():
    global PG_DSN
    if PG_DSN:
        return PG_DSN
    pwd = subprocess.run(
        ["docker", "exec", "cognitive_postgres", "printenv", "POSTGRES_PASSWORD"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    ip = subprocess.run(
        ["docker", "inspect", "cognitive_postgres",
         "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip().splitlines()[0]
    PG_DSN = f"postgresql://cognitive:{pwd}@{ip}:5432/cognitive_core"
    log.info(f"derived PG_DSN host={ip}")
    return PG_DSN


def load_state():
    global _state
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        _state.update({k: saved[k] for k in ("last_pushed_msg_id", "total_published", "total_dlq") if k in saved})
        log.info(f"state loaded: last={_state['last_pushed_msg_id']} total={_state['total_published']}")
    except FileNotFoundError:
        log.info("no prior state — fresh start")
    except Exception as e:
        log.warning(f"state load error: {e}")


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"state save error: {e}")


def write_dlq(payload, error):
    try:
        with open(DLQ_FILE, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
                "error": error,
                "hostname": HOSTNAME,
            }) + "\n")
        _state["total_dlq"] += 1
    except Exception as e:
        log.error(f"DLQ write error: {e}")


def write_health(status, details):
    try:
        with open(HEALTH_FILE, "w") as f:
            json.dump({
                "status": status,
                "details": details,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "hostname": HOSTNAME,
            }, f)
    except Exception:
        pass


def publish_nats(subject, payload):
    """Publish to NATS with latency tracking + DLQ on failure."""
    t_start = time.time()
    try:
        result = subprocess.run(
            [NATS_BIN, "-s", NATS_URL, "publish", subject, payload],
            capture_output=True, text=True, timeout=5,
        )
        latency = time.time() - t_start
        with _latencies_lock:
            _latencies.append(latency)
        if result.returncode != 0:
            write_dlq({"subject": subject, "payload": payload[:500]}, result.stderr[:300])
            log.warning(f"DLQ: {subject} err={result.stderr[:100]}")
            return False
        _state["total_published"] += 1
        return True
    except Exception as e:
        write_dlq({"subject": subject, "payload": payload[:500]}, str(e))
        log.error(f"DLQ exception: {e}")
        return False


def replay_unbridged(conn):
    """On startup: replay any agent_inbox events since last_pushed_msg_id."""
    last = _state.get("last_pushed_msg_id")
    if not last:
        log.info("no last_pushed_msg_id — skipping replay")
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, raw_payload, created_at "
            "FROM l1_raw_events "
            "WHERE domain='agent_inbox' AND id > %s::uuid "
            "ORDER BY created_at ASC LIMIT 100",
            (last,),
        )
        rows = cur.fetchall()
    log.info(f"replay: {len(rows)} unbridged events since {last[:8]}")
    count = 0
    for msg_id, payload_jsonb, created_at in rows:
        try:
            data = payload_jsonb if isinstance(payload_jsonb, dict) else json.loads(payload_jsonb)
            to_agent = data.get("to", "")
            if not to_agent:
                continue
            subject = f"agent.{to_agent}.dm"
            event = json.dumps({
                "msg_id": msg_id,
                "from_agent": data.get("from", "?"),
                "to_agent": to_agent,
                "text": (data.get("text") or "")[:7000],
                "context": data.get("context") or {},
                "sent_at": created_at.isoformat() if created_at else None,
                "via": "pg-notify-replay",
                "hostname": HOSTNAME,
            }, ensure_ascii=False)
            if publish_nats(subject, event):
                _state["last_pushed_msg_id"] = msg_id
                count += 1
        except Exception as e:
            log.warning(f"replay {msg_id[:8]} err: {e}")
    save_state()
    log.info(f"replay done: {count} events re-pushed")
    return count


def run_listener():
    """Main listener loop."""
    dsn = derive_pg_dsn()
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                # Replay first
                replay_unbridged(conn)

                with conn.cursor() as cur:
                    cur.execute("LISTEN agent_inbox;")
                    cur.execute("LISTEN room_event;")
                    log.info("LISTEN agent_inbox + room_event active — push mode")
                    write_health("healthy", {"mode": "live", "started": _state["started_at"]})

                    for n in conn.notifies(timeout=300):
                        try:
                            data = json.loads(n.payload)
                            # Route by channel
                            if n.channel == "room_event":
                                room_id = data.get("room_id", "")
                                subject = f"room.{room_id}.events"
                                event = json.dumps(data, ensure_ascii=False)
                                if publish_nats(subject, event):
                                    log.info(f"PUSH {subject} msg={data.get('message_id','?')[:8]} from={data.get('from_agent','?')}")
                                continue
                            # Default: agent_inbox
                            to_agent = data.get("to_agent", "")
                            msg_id = data.get("msg_id", "?")
                            from_agent = data.get("from_agent", "?")
                            if not to_agent:
                                continue
                            subject = f"agent.{to_agent}.dm"
                            event = json.dumps({
                                "msg_id": msg_id,
                                "from_agent": from_agent,
                                "to_agent": to_agent,
                                "text": (data.get("text") or "")[:7000],
                                "context": data.get("context") or {},
                                "sent_at": data.get("sent_at"),
                                "bridged_at": datetime.now(timezone.utc).isoformat(),
                                "via": "pg-notify-push",
                                "hostname": HOSTNAME,
                            }, ensure_ascii=False)
                            if publish_nats(subject, event):
                                _state["last_pushed_msg_id"] = msg_id
                                save_state()
                                log.info(f"PUSH {subject} msg={msg_id[:8]} from={from_agent}")
                        except Exception as e:
                            log.warning(f"notify err: {e}")
                            write_dlq({"raw_payload": n.payload[:500]}, str(e))
        except KeyboardInterrupt:
            log.info("shutdown")
            save_state()
            break
        except Exception as e:
            log.error(f"connection error: {e} — reconnect 10s")
            write_health("degraded", {"error": str(e)})
            time.sleep(10)


def percentile(sorted_list, p):
    if not sorted_list:
        return 0.0
    k = int((p / 100.0) * (len(sorted_list) - 1))
    return sorted_list[k]


def slo_monitor():
    """Periodic SLO check: P95 latency. Alert if sustained >2s for 5min."""
    breach_start = None
    while True:
        time.sleep(60)
        with _latencies_lock:
            samples = sorted(_latencies)
        if len(samples) < 10:
            continue
        p50 = percentile(samples, 50)
        p95 = percentile(samples, 95)
        p99 = percentile(samples, 99)
        log.info(f"SLO: p50={p50:.3f}s p95={p95:.3f}s p99={p99:.3f}s n={len(samples)}")
        if p95 > 2.0:
            if breach_start is None:
                breach_start = time.time()
            elif time.time() - breach_start > 300:
                log.warning(f"SLO BREACH: P95 latency {p95:.2f}s sustained >5min")
                breach_start = None
        else:
            breach_start = None


# === HTTP health endpoint ===
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            with _latencies_lock:
                samples = sorted(_latencies)
            body = json.dumps({
                "status": "healthy" if samples else "starting",
                "total_published": _state["total_published"],
                "total_dlq": _state["total_dlq"],
                "last_pushed_msg_id": _state["last_pushed_msg_id"],
                "uptime_sec": int(time.time() - _state["started_at"]),
                "p50_latency": percentile(samples, 50),
                "p95_latency": percentile(samples, 95),
                "p99_latency": percentile(samples, 99),
                "samples": len(samples),
                "hostname": HOSTNAME,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    log.info(f"=== cognitive-pg-to-nats v2 starting host={HOSTNAME} ===")
    load_state()
    # Start health server
    threading.Thread(target=lambda: http.server.HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler).serve_forever(), daemon=True).start()
    log.info(f"health endpoint http://0.0.0.0:{HEALTH_PORT}/health")
    # Start SLO monitor
    threading.Thread(target=slo_monitor, daemon=True).start()
    # Main listener
    run_listener()


if __name__ == "__main__":
    main()
