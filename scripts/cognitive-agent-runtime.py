#!/usr/bin/env python3
# Cognitive Agent Runtime v2 (Phase 2 — tool calling).
# Server-side persistent agent runtime — wakes every poll_interval, reads inbox,
# matches DM text against trigger patterns from persona configs in L1, takes action:
# silent / auto_ack / llm_reply (with tool calling) / escalate.
# Persona configurable through cognitive memory.
# v2 ADD: DeepSeek function calling with whitelisted tools for factual replies.

import os, sys, json, re, time, logging, urllib.request, urllib.error, subprocess
from datetime import datetime, timezone

ENDPOINT = "https://mcp.xn----8sbwawqx4fza.xn--p1ai"
LOG_FILE = "/var/log/cognitive-agent-runtime.log"
HISTORY_DIR = "/var/run/cognitive/agent-history"
PERSONA_REFRESH_SEC = 60  # was 300; lower so UI channel/standin changes apply within ~1 min
DEFAULT_POLL_SEC = 5
NOTIFY_BIN = "/usr/local/bin/cognitive-notify.sh"
TOOL_TIMEOUT_SEC = 10
MAX_TOOL_CALLS_PER_REPLY = 3

# Agent API keys are resolved dynamically from the agent_keys table via
# resolve_agent_key() — the DB is the single source of truth, so the daemon can
# act for ANY onboarded agent (opt-in via agent_states.standin_enabled), not a
# fixed list. This dict is an OPTIONAL emergency fallback, kept EMPTY so no
# secrets live in this (git-tracked) source.
AGENT_KEYS = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cogcore-agent-runtime")
os.makedirs(HISTORY_DIR, exist_ok=True)


def load_deepseek_env():
    env = {}
    try:
        result = subprocess.run(
            ["docker", "exec", "cognitive_api", "printenv"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
                    env[k] = v
    except Exception as e:
        log.warning(f"DeepSeek env load failed: {e}")
    return env


DS_ENV = load_deepseek_env()


def _urlopen_retry(req, timeout, attempts=3):
    """urlopen with retry on TRANSIENT network/DNS errors (URLError such as the
    intermittent '.рф' "No address associated with hostname" flap from the host).
    HTTPError (real 4xx/5xx responses) is NOT retried. Backoff 0.8s, 1.6s."""
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError:
            raise  # a real HTTP response — do not retry
        except urllib.error.URLError as e:
            last = e
            if i < attempts - 1:
                time.sleep(0.8 * (i + 1))
    raise last


def http_get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    return _urlopen_retry(req, timeout)


def http_post(url, payload, headers=None, timeout=15):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    return _urlopen_retry(req, timeout)


# === TOOLS (whitelisted) ===
# Each tool returns string result (truncated to ~2000 chars).
# Security: subprocess args are LIST (never shell=True). No user input concat into args.

def _run(args, timeout=TOOL_TIMEOUT_SEC):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        out = (r.stdout + ("\n[stderr]\n" + r.stderr if r.stderr else ""))[:2000]
        return out or "(empty)"
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


def tool_disk_usage(path: str = "/") -> str:
    """Disk usage. path must be one of allowed mounts."""
    allowed = {"/", "/mnt/cold-storage", "/boot"}
    if path not in allowed:
        return f"ERROR: path {path} not in whitelist {sorted(allowed)}"
    return _run(["df", "-h", path])


def tool_docker_ps() -> str:
    """List running Docker containers."""
    return _run(["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"])


def tool_docker_logs(container: str, lines: int = 30) -> str:
    """Tail container logs. container must be a known production container."""
    allowed = {
        "cognitive_api", "cognitive_mcp", "cognitive_postgres", "cognitive_redis",
        "cognitive_minio", "cognitive_nginx", "cognitive_nats", "cognitive_backup",
        "ai-crm-backend", "ai-crm-frontend",
        "monitoring_prometheus", "monitoring_grafana", "monitoring_loki",
    }
    if container not in allowed:
        return f"ERROR: container {container} not in whitelist"
    n = min(max(int(lines), 1), 200)
    return _run(["docker", "logs", container, "--tail", str(n)])


def tool_postgres_query(database: str, query: str) -> str:
    """Run safe Postgres query (SELECT only). database must be known."""
    allowed_db = {"cognitive_core", "ai_crm", "ai_crm_staging"}
    if database not in allowed_db:
        return f"ERROR: database {database} not allowed"
    if not query.strip().upper().startswith("SELECT"):
        return "ERROR: only SELECT queries allowed"
    if len(query) > 2000:
        return "ERROR: query too long"
    return _run(["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
                 "-d", database, "-A", "-c", query])


def tool_git_log(repo: str = "/opt/cognitive-core", lines: int = 5) -> str:
    """Recent commits from a known repo."""
    allowed = {"/opt/cognitive-core", "/opt/ai-crm"}
    if repo not in allowed:
        return f"ERROR: repo {repo} not whitelisted"
    n = min(max(int(lines), 1), 50)
    try:
        r = subprocess.run(["git", "-C", repo, "log", "--oneline", f"-{n}"],
                           capture_output=True, text=True, timeout=TOOL_TIMEOUT_SEC, check=False)
        return r.stdout[:2000] or "(empty)"
    except Exception as e:
        return f"ERROR: {e}"


def tool_systemctl_status(service: str) -> str:
    """systemctl status for known cogcore services."""
    allowed = {
        "cognitive-deploy.timer", "cognitive-agent-runtime",
        "fail2ban", "cron", "nginx", "docker",
    }
    if service not in allowed:
        return f"ERROR: service {service} not whitelisted"
    return _run(["systemctl", "status", service, "--no-pager", "--lines", "5"])


def tool_cogcore_bb(subcommand: str = "online") -> str:
    """cogcore-bb L0 blackboard helper. subcommand: online, info, list."""
    allowed = {"online", "info", "list"}
    if subcommand not in allowed:
        return f"ERROR: subcommand {subcommand} not allowed (use one of {sorted(allowed)})"
    return _run(["cogcore-bb", subcommand])


def tool_cognitive_recall(query: str, domain: str = "general") -> str:
    """Recall from L3 memory via cognitive-core API."""
    if len(query) > 500:
        return "ERROR: query too long"
    try:
        url = f"{ENDPOINT}/recall"
        payload = {"query": query, "domain": domain, "top_k": 3}
        d = http_post(url, payload, headers={"X-API-Key": AGENT_KEYS["cognitive-core-laptop"]}, timeout=15)
        return json.dumps(d, ensure_ascii=False)[:2000]
    except Exception as e:
        return f"ERROR: {e}"


def tool_uptime_loadavg() -> str:
    """System uptime + load averages."""
    return _run(["uptime"])


def tool_free_memory() -> str:
    """Memory usage."""
    return _run(["free", "-h"])


# === ai-crm-specific tools (added by request from ai-crm-deploy 2026-05-09) ===

def tool_docker_exec(container: str, command: str) -> str:
    """Run safe whitelisted command inside a known container."""
    allowed_containers = {
        "ai-crm-backend", "ai-crm-frontend",
        "cognitive_api", "cognitive_mcp",
    }
    if container not in allowed_containers:
        return f"ERROR: container {container} not in whitelist"
    # Whitelist commands by exact prefix
    allowed_cmd_prefixes = (
        "alembic current",
        "alembic history",
        "alembic heads",
        "alembic show",
        "npm test",
        "npm run lint",
        "pytest --collect-only",
        "python -V",
        "node -v",
        "ls /app",
        "cat /app/version.txt",
        "env",  # для проверки конфигов (non-secret вылезут sed-ом, но ладно)
    )
    cmd_clean = command.strip()
    if not any(cmd_clean.startswith(p) for p in allowed_cmd_prefixes):
        return f"ERROR: command not in whitelist. Allowed prefixes: {list(allowed_cmd_prefixes)}"
    # Split safely (no shell)
    return _run(["docker", "exec", container] + cmd_clean.split())


def tool_alembic_history(repo: str = "/opt/ai-crm") -> str:
    """Show Alembic migration history (last 10)."""
    if repo not in {"/opt/ai-crm", "/opt/ai-crm-staging"}:
        return f"ERROR: repo {repo} not allowed"
    # Run via docker exec на backend container
    container = "ai-crm-backend"
    return _run(["docker", "exec", container, "alembic", "history", "--verbose"], timeout=15)


def tool_curl_health(url_path: str) -> str:
    """Curl localhost-only health endpoints. url_path must start with allowed prefix."""
    allowed_endpoints = {
        "http://localhost:8080/health",
        "http://localhost:8080/api/health",
        "http://localhost:8080/api/health/db",
        "http://localhost:8080/api/health/s3",
        "http://localhost:8080/api/health/redis",
        "http://localhost:8080/api/health/llm",
        "http://localhost:8080/api/metrics",
        "http://localhost:8000/health",  # cognitive_api
        "http://localhost:9090/-/healthy",  # prometheus
        "http://localhost:3001/api/health",  # grafana
        "http://localhost:3100/ready",  # loki
    }
    if url_path not in allowed_endpoints:
        return f"ERROR: url not in whitelist. Allowed: {sorted(allowed_endpoints)}"
    return _run(["curl", "-fsS", "-m", "5", url_path])


def tool_pg_db_size(database: str = "ai_crm") -> str:
    """Show database size and top 5 largest tables."""
    allowed_db = {"cognitive_core", "ai_crm", "ai_crm_staging"}
    if database not in allowed_db:
        return f"ERROR: database {database} not allowed"
    query = (
        f"SELECT pg_size_pretty(pg_database_size('{database}')) as db_size; "
        "SELECT schemaname || '.' || relname as table, "
        "pg_size_pretty(pg_total_relation_size(relid)) as size "
        "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 5;"
    )
    return _run(["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
                 "-d", database, "-c", query])


def tool_pg_active_connections(database: str = "ai_crm") -> str:
    """Show active connections per database."""
    allowed_db = {"cognitive_core", "ai_crm", "ai_crm_staging", "all"}
    if database not in allowed_db:
        return f"ERROR: database {database} not allowed"
    if database == "all":
        query = "SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname ORDER BY count DESC;"
        return _run(["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
                     "-d", "cognitive_core", "-c", query])
    query = f"SELECT count(*), state FROM pg_stat_activity WHERE datname='{database}' GROUP BY state;"
    return _run(["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
                 "-d", database, "-c", query])


# === Phase B.1: WRITE tools with safety gate ===
# Each write tool requires [APPROVED] tag or trusted_sender in calling DM (server-side check).
# Audit log: /var/log/cognitive-write-tools.log
# Pending operations queued in Redis bb:pending:write_op:<uuid> for owner approval if needed.

WRITE_TOOLS_AUDIT = "/var/log/cognitive-write-tools.log"
TRUSTED_SENDERS = {"cognitive-core-laptop", "ai-crm-deploy"}  # whitelist agents that can request writes


def _audit_write(tool: str, args: dict, result: str, status: str = "ok"):
    import time as _time
    ts = _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())
    line = f"[{ts}] {status} {tool}({args}) → {result[:200]}\n"
    try:
        with open(WRITE_TOOLS_AUDIT, "a") as f:
            f.write(line)
    except Exception:
        pass


def tool_docker_restart(container: str, reason: str = "") -> str:
    """Restart a known container. Requires reason ≥10 chars for audit."""
    allowed = {
        "cognitive_api", "cognitive_mcp", "cognitive_nginx", "cognitive_redis",
        "ai-crm-backend", "ai-crm-frontend",
        "monitoring_grafana", "monitoring_prometheus",
        # Excluded by design: cognitive_postgres (data risk), cognitive_minio (data risk), cognitive_nats (state)
    }
    if container not in allowed:
        msg = f"ERROR: container {container} not in write-whitelist {sorted(allowed)}"
        _audit_write("docker_restart", {"container": container}, msg, "denied")
        return msg
    if len(reason) < 10:
        msg = "ERROR: reason must be ≥10 chars (audit requirement)"
        _audit_write("docker_restart", {"container": container, "reason": reason}, msg, "denied")
        return msg
    result = _run(["docker", "restart", container], timeout=30)
    _audit_write("docker_restart", {"container": container, "reason": reason}, result)
    return result


def tool_systemctl_restart(service: str, reason: str = "") -> str:
    """Restart whitelisted systemd unit. Reason ≥10 chars."""
    allowed = {
        "cognitive-agent-runtime", "cognitive-inbox-to-nats", "cognitive-pg-to-nats",
        "fail2ban",  # safe restart
    }
    if service not in allowed:
        msg = f"ERROR: service {service} not in write-whitelist {sorted(allowed)}"
        _audit_write("systemctl_restart", {"service": service}, msg, "denied")
        return msg
    if len(reason) < 10:
        msg = "ERROR: reason must be ≥10 chars"
        _audit_write("systemctl_restart", {"service": service, "reason": reason}, msg, "denied")
        return msg
    result = _run(["sudo", "-n", "systemctl", "restart", service], timeout=30)
    _audit_write("systemctl_restart", {"service": service, "reason": reason}, result)
    return result


def tool_nginx_reload(reason: str = "") -> str:
    """Reload nginx config (no restart, no downtime). Reason ≥10 chars."""
    if len(reason) < 10:
        msg = "ERROR: reason must be ≥10 chars"
        _audit_write("nginx_reload", {"reason": reason}, msg, "denied")
        return msg
    # Test config first
    test = _run(["docker", "exec", "cognitive_nginx", "nginx", "-t"], timeout=10)
    if "syntax is ok" not in test and "test is successful" not in test:
        msg = f"ERROR: nginx -t failed, refusing reload: {test[:200]}"
        _audit_write("nginx_reload", {"reason": reason}, msg, "denied")
        return msg
    result = _run(["docker", "exec", "cognitive_nginx", "nginx", "-s", "reload"], timeout=10)
    _audit_write("nginx_reload", {"reason": reason}, result)
    return f"nginx -t OK, reloaded: {result}"


def tool_git_pull(repo: str = "/opt/cognitive-core", reason: str = "") -> str:
    """git pull --ff-only on whitelisted repo. Reason ≥10 chars."""
    allowed = {"/opt/cognitive-core", "/opt/ai-crm"}
    if repo not in allowed:
        return f"ERROR: repo {repo} not whitelisted"
    if len(reason) < 10:
        return "ERROR: reason must be ≥10 chars"
    result = _run(["git", "-C", repo, "pull", "--ff-only"], timeout=30)
    _audit_write("git_pull", {"repo": repo, "reason": reason}, result)
    return result


def tool_clear_old_snapshots(days: int = 90, reason: str = "") -> str:
    """Delete cold-storage backup snapshots older than N days (default 90, min 30)."""
    if days < 30:
        return "ERROR: days must be ≥30 (safety floor)"
    if len(reason) < 10:
        return "ERROR: reason must be ≥10 chars"
    cmd = f"find /mnt/cold-storage/snapshots -name 'cognitive-snapshot-*.tar.gz' -mtime +{days} -delete -print"
    result = _run(["bash", "-c", cmd], timeout=30)
    _audit_write("clear_old_snapshots", {"days": days, "reason": reason}, result)
    return result or "(no files matched)"


# Tool registry: name -> (function, schema for DeepSeek function calling)
TOOL_REGISTRY = {
    "disk_usage": (tool_disk_usage, {
        "type": "function",
        "function": {
            "name": "disk_usage",
            "description": "Get disk usage for a mount point. Path must be one of: /, /mnt/cold-storage, /boot",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "/"}},
                "required": [],
            },
        },
    }),
    "docker_ps": (tool_docker_ps, {
        "type": "function",
        "function": {
            "name": "docker_ps",
            "description": "List running Docker containers with name, status, image",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }),
    "docker_logs": (tool_docker_logs, {
        "type": "function",
        "function": {
            "name": "docker_logs",
            "description": "Tail logs of a known production container (cognitive_*, ai-crm-*, monitoring_*)",
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {"type": "string", "description": "Container name from whitelist"},
                    "lines": {"type": "integer", "default": 30, "description": "Number of lines"},
                },
                "required": ["container"],
            },
        },
    }),
    "postgres_query": (tool_postgres_query, {
        "type": "function",
        "function": {
            "name": "postgres_query",
            "description": "Execute SELECT query against a known database (cognitive_core, ai_crm, ai_crm_staging)",
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {"type": "string"},
                    "query": {"type": "string", "description": "SELECT statement only"},
                },
                "required": ["database", "query"],
            },
        },
    }),
    "git_log": (tool_git_log, {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Recent commits from /opt/cognitive-core or /opt/ai-crm",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "default": "/opt/cognitive-core"},
                    "lines": {"type": "integer", "default": 5},
                },
                "required": [],
            },
        },
    }),
    "systemctl_status": (tool_systemctl_status, {
        "type": "function",
        "function": {
            "name": "systemctl_status",
            "description": "Status of known systemd unit",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    }),
    "cogcore_bb": (tool_cogcore_bb, {
        "type": "function",
        "function": {
            "name": "cogcore_bb",
            "description": "L0 blackboard query: online (active agents), info (stats), list (keys)",
            "parameters": {
                "type": "object",
                "properties": {"subcommand": {"type": "string", "default": "online"}},
                "required": [],
            },
        },
    }),
    "cognitive_recall": (tool_cognitive_recall, {
        "type": "function",
        "function": {
            "name": "cognitive_recall",
            "description": "Recall relevant knowledge from L3 master memory by semantic query",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "domain": {"type": "string", "default": "general"},
                },
                "required": ["query"],
            },
        },
    }),
    "uptime_loadavg": (tool_uptime_loadavg, {
        "type": "function",
        "function": {
            "name": "uptime_loadavg",
            "description": "System uptime and load averages (1, 5, 15 min)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }),
    "free_memory": (tool_free_memory, {
        "type": "function",
        "function": {
            "name": "free_memory",
            "description": "Memory usage (total/used/free/cache)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }),
    "docker_exec": (tool_docker_exec, {
        "type": "function",
        "function": {
            "name": "docker_exec",
            "description": "Run a whitelisted command inside a known container. Allowed prefixes: alembic current/history/heads/show, npm test, npm run lint, pytest --collect-only, python -V, node -v, ls /app, env",
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {"type": "string", "description": "ai-crm-backend, ai-crm-frontend, cognitive_api, cognitive_mcp"},
                    "command": {"type": "string", "description": "Command starting with allowed prefix"},
                },
                "required": ["container", "command"],
            },
        },
    }),
    "alembic_history": (tool_alembic_history, {
        "type": "function",
        "function": {
            "name": "alembic_history",
            "description": "Show Alembic migration history for ai-crm",
            "parameters": {
                "type": "object",
                "properties": {"repo": {"type": "string", "default": "/opt/ai-crm"}},
                "required": [],
            },
        },
    }),
    "curl_health": (tool_curl_health, {
        "type": "function",
        "function": {
            "name": "curl_health",
            "description": "Curl localhost-only health endpoints (whitelisted URLs for ai-crm, cognitive_api, prometheus, grafana, loki)",
            "parameters": {
                "type": "object",
                "properties": {"url_path": {"type": "string", "description": "Full URL from whitelist"}},
                "required": ["url_path"],
            },
        },
    }),
    "pg_db_size": (tool_pg_db_size, {
        "type": "function",
        "function": {
            "name": "pg_db_size",
            "description": "Show Postgres database size and top 5 largest tables",
            "parameters": {
                "type": "object",
                "properties": {"database": {"type": "string", "default": "ai_crm"}},
                "required": [],
            },
        },
    }),
    "pg_active_connections": (tool_pg_active_connections, {
        "type": "function",
        "function": {
            "name": "pg_active_connections",
            "description": "Show active Postgres connections (per state for one db, or count per db with database='all')",
            "parameters": {
                "type": "object",
                "properties": {"database": {"type": "string", "default": "ai_crm"}},
                "required": [],
            },
        },
    }),
    # === Phase B.1 WRITE TOOLS (require reason ≥10 chars, audit logged) ===
    "docker_restart": (tool_docker_restart, {
        "type": "function",
        "function": {
            "name": "docker_restart",
            "description": "[WRITE] Restart container. Whitelisted: cognitive_api, cognitive_mcp, cognitive_nginx, cognitive_redis, ai-crm-backend, ai-crm-frontend, monitoring_grafana, monitoring_prometheus. Requires reason ≥10 chars. Excluded for safety: postgres, minio, nats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {"type": "string"},
                    "reason": {"type": "string", "description": "Why restart needed (≥10 chars audit)"}
                },
                "required": ["container", "reason"],
            },
        },
    }),
    "systemctl_restart": (tool_systemctl_restart, {
        "type": "function",
        "function": {
            "name": "systemctl_restart",
            "description": "[WRITE] Restart systemd unit (cognitive-agent-runtime, cognitive-inbox-to-nats, cognitive-pg-to-nats, fail2ban). Reason ≥10 chars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["service", "reason"],
            },
        },
    }),
    "nginx_reload": (tool_nginx_reload, {
        "type": "function",
        "function": {
            "name": "nginx_reload",
            "description": "[WRITE] Reload nginx config (graceful, no downtime). Tests config first, refuses if invalid. Reason ≥10 chars.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    }),
    "git_pull": (tool_git_pull, {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": "[WRITE] git pull --ff-only on whitelisted repo (/opt/cognitive-core, /opt/ai-crm). Reason ≥10 chars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "default": "/opt/cognitive-core"},
                    "reason": {"type": "string"}
                },
                "required": ["reason"],
            },
        },
    }),
    "clear_old_snapshots": (tool_clear_old_snapshots, {
        "type": "function",
        "function": {
            "name": "clear_old_snapshots",
            "description": "[WRITE] Delete cold-storage memory snapshots older than N days (min 30). Reason ≥10 chars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 90},
                    "reason": {"type": "string"}
                },
                "required": ["reason"],
            },
        },
    }),
}


def get_tools_for_persona(persona):
    """Return tools list per persona allowed_tools (or all if not specified)."""
    allowed = persona.get("allowed_tools")
    if allowed is None:
        return [s for _, s in TOOL_REGISTRY.values()]
    return [TOOL_REGISTRY[t][1] for t in allowed if t in TOOL_REGISTRY]


def execute_tool(name, args):
    """Run a whitelisted tool with given args."""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return f"ERROR: unknown tool {name}"
    fn, _schema = entry
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return f"ERROR: bad args for {name}: {e}"
    except Exception as e:
        return f"ERROR: tool {name} crashed: {e}"


def load_personas():
    try:
        result = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
             "-d", "cognitive_core", "-t", "-A", "-c",
             "SELECT raw_payload::text FROM l1_raw_events WHERE domain='agent_persona' ORDER BY created_at DESC"],
            capture_output=True, text=True, timeout=10,
        )
        personas = {}
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                p = json.loads(line)
                pid = p.get("persona_id")
                if not pid or not p.get("active", True):
                    continue
                if pid not in personas:
                    personas[pid] = p
            except Exception as e:
                log.warning(f"persona parse error: {e}")
        return personas
    except Exception as e:
        log.error(f"load_personas failed: {e}")
        return {}


def load_standin_agents():
    """Return {agent_id: agent_label} for agents opted in to the 24/7 stand-in
    (agent_states.standin_enabled = true). The daemon answers only for these."""
    try:
        result = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
             "-d", "cognitive_core", "-t", "-A", "-F", "|", "-c",
             "SELECT agent_id, COALESCE(agent_label,'') FROM agent_states "
             "WHERE standin_enabled = true"],
            capture_output=True, text=True, timeout=10,
        )
        out = {}
        for line in (result.stdout or "").strip().splitlines():
            if "|" in line:
                aid, label = line.split("|", 1)
                aid = aid.strip()
                if aid:
                    out[aid] = label.strip()
        return out
    except Exception as e:
        log.error(f"load_standin_agents failed: {e}")
        return {}


_CONFIG_KEY_CACHE = {}


def _decrypt_config(cfg):
    """Decrypt a per-agent channel config the app encrypted ({'_enc': token},
    Fernet/COGCORE_CONFIG_KEY). Plaintext configs (no '_enc') pass through. The key
    is read once from the api container env (where the app keeps it)."""
    if not isinstance(cfg, dict) or "_enc" not in cfg:
        return cfg
    key = _CONFIG_KEY_CACHE.get("k")
    if key is None:
        try:
            r = subprocess.run(
                ["docker", "exec", "cognitive_api", "printenv", "COGCORE_CONFIG_KEY"],
                capture_output=True, text=True, timeout=5)
            key = (r.stdout or "").strip()
        except Exception:
            key = ""
        _CONFIG_KEY_CACHE["k"] = key
    if not key:
        log.warning("no COGCORE_CONFIG_KEY -> cannot decrypt channel config")
        return {}
    try:
        from cryptography.fernet import Fernet
        return json.loads(Fernet(key.encode()).decrypt(cfg["_enc"].encode()).decode())
    except Exception as e:
        log.error(f"channel config decrypt failed: {e}")
        return {}


def load_agent_channel(agent_id):
    """Return (wake_channel, config_dict) for an agent: channel from agent_states,
    secret config (routine fire_url+token / managed key) from agent_channel_config.
    Defaults to ('deepseek', {}). agent_id is format-validated (no SQL injection)."""
    if not agent_id or not re.match(r"^[\w\-]+$", agent_id):
        return "deepseek", {}
    channel, cfg = "deepseek", {}
    try:
        r = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
             "-d", "cognitive_core", "-t", "-A", "-c",
             "SELECT wake_channel FROM agent_states WHERE agent_id='" + agent_id + "'"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or "").strip()
        if out:
            channel = out.splitlines()[0].strip() or "deepseek"
        if channel and channel != "deepseek":
            rc = subprocess.run(
                ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
                 "-d", "cognitive_core", "-t", "-A", "-c",
                 "SELECT config::text FROM agent_channel_config WHERE agent_id='" + agent_id + "'"],
                capture_output=True, text=True, timeout=10,
            )
            cout = (rc.stdout or "").strip()
            if cout:
                try:
                    cfg = json.loads(cout.splitlines()[0])
                    cfg = _decrypt_config(cfg)
                except Exception:
                    cfg = {}
    except Exception as e:
        log.warning(f"load_agent_channel failed for {agent_id}: {e}")
    return channel, cfg


def default_persona(agent_id, label):
    """Context-aware default stand-in persona for an opted-in agent that has no
    custom agent_persona config. Replies via DeepSeek and MAY call cognitive_recall
    (owner memory) for context. allowed_tools is restricted to the read-only recall
    tool — a generic stand-in must NOT get ops tools (docker/git/restart)."""
    name = label or agent_id
    return {
        "persona_id": agent_id,
        "active": True,
        "triggers": [{"pattern": r"\S", "action": "llm_reply", "priority": 50}],
        "poll_interval_seconds": DEFAULT_POLL_SEC,
        "allowed_tools": ["cognitive_recall"],
        "auto_ack_template": f"{name}: на связи (отвечаю вместо Claude — он сейчас офлайн).",
        "llm_settings": {
            "model": "deepseek-chat",
            "max_tokens": 800,
            "temperature": 0.3,
            "system_prompt": (
                f"Ты — {name}, ИИ-ассистент владельца. Ты отвечаешь за него в чатах и "
                f"комнатах 24/7, ПОКА его основной агент Claude офлайн. Отвечай кратко, "
                f"по делу, на русском. Если вопрос требует контекста о проектах и делах "
                f"владельца — ВЫЗОВИ инструмент cognitive_recall и опирайся на найденную "
                f"память. Не выдумывай факты: если в памяти нет — честно скажи об этом."
            ),
        },
    }


_AGENT_KEY_CACHE = {}


def resolve_agent_key(agent_id):
    """Resolve an agent's API key from the agent_keys table (single source of
    truth), cached in-process — like resolve_room_key for room keys. Replaces the
    old hardcoded AGENT_KEYS so the daemon can act for ANY onboarded agent.
    agent_id is format-validated before string interpolation (no injection)."""
    if not agent_id or not re.match(r"^[\w\-]+$", agent_id):
        return None
    if agent_id in _AGENT_KEY_CACHE:
        return _AGENT_KEY_CACHE[agent_id]
    key = None
    try:
        result = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
             "-d", "cognitive_core", "-t", "-A", "-c",
             "SELECT api_key FROM agent_keys WHERE agent_id='" + agent_id + "' "
             "AND revoked_at IS NULL ORDER BY last_used_at DESC NULLS LAST, "
             "created_at DESC LIMIT 1"],
            capture_output=True, text=True, timeout=10,
        )
        out = (result.stdout or "").strip()
        if out:
            key = out.splitlines()[0].strip()
    except Exception as e:
        log.warning(f"resolve_agent_key failed for {agent_id}: {e}")
    if not key:
        key = AGENT_KEYS.get(agent_id)  # emergency fallback (normally empty)
    if key:
        _AGENT_KEY_CACHE[agent_id] = key
    return key


def load_inbox(persona_id, since_minutes=60):
    key = resolve_agent_key(persona_id)
    if not key:
        return []
    try:
        url = f"{ENDPOINT}/agents/inbox?since_minutes={since_minutes}&limit=20"
        d = http_get(url, headers={"X-API-Key": key})
        return d.get("messages", []) or []
    except Exception as e:
        log.warning(f"inbox load failed for {persona_id}: {e}")
        return []


def send_dm(from_persona, to_agent, text, context=None, parent_id=None):
    key = resolve_agent_key(from_persona)
    if not key:
        return None
    # prefix removed 2026-05-13 — sender visible via msg.from field
    payload = {"to": to_agent, "text": text, "context": context or {}}
    if parent_id:
        payload["context"]["parent_id"] = parent_id
    try:
        url = f"{ENDPOINT}/agents/message"
        d = http_post(url, payload, headers={"X-API-Key": key})
        return d.get("id") or d.get("message_id")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        log.error(f"send_dm failed {from_persona}->{to_agent}: HTTP {e.code} {body}")
        return None
    except Exception as e:
        log.error(f"send_dm failed {from_persona}->{to_agent}: {e}")
        return None


# === Reverse room bridge (2026-06-02) ===
# When an incoming inbox message carries context.via == "room", the daemon posts
# its reply BACK INTO that room (POST /rooms/<id>/post, X-Room-Key auth) instead
# of DM-ing the owner's private inbox. The room api_key is looked up from the
# `rooms` table via the same `docker exec cognitive_postgres psql` path the
# daemon already uses for personas (it runs on the host as root). Keys are cached
# in-process to avoid a psql round-trip on every reply.
_ROOM_KEY_CACHE = {}


def resolve_room_key(room_id):
    """Return the room's api_key (for X-Room-Key) by room_id, or None.

    Looked up via docker exec psql against the rooms table; cached in-process.
    UUID-validated to keep it out of the SQL literal cleanly.
    """
    if not room_id:
        return None
    if room_id in _ROOM_KEY_CACHE:
        return _ROOM_KEY_CACHE[room_id]
    if not re.fullmatch(r"[0-9a-fA-F\-]{36}", str(room_id)):
        log.warning(f"resolve_room_key: bad room_id format {room_id!r}")
        return None
    try:
        result = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
             "-d", "cognitive_core", "-t", "-A", "-c",
             f"SELECT api_key FROM rooms WHERE id='{room_id}'::uuid"],
            capture_output=True, text=True, timeout=10,
        )
        key = (result.stdout or "").strip()
        if key:
            _ROOM_KEY_CACHE[room_id] = key
            return key
        log.warning(f"resolve_room_key: no api_key for room {room_id}")
        return None
    except Exception as e:
        log.error(f"resolve_room_key failed for {room_id}: {e}")
        return None


def post_to_room(room_id, from_agent, text):
    """Post `text` from `from_agent` into the room. Returns message id or None.

    Best-effort: any failure (key not resolvable, HTTP error) is logged and
    swallowed so a reverse-bridge failure never breaks the normal reply path.
    """
    try:
        key = resolve_room_key(room_id)
        if not key:
            log.warning(f"post_to_room: no room key for {room_id}, skipping room post")
            return None
        url = f"{ENDPOINT}/rooms/{room_id}/post"
        d = http_post(url, {"from_agent": from_agent, "text": text},
                      headers={"X-Room-Key": key})
        mid = d.get("id") or d.get("message_id")
        log.info(f"[{from_agent}] ROOM_REPLY -> room {room_id} msg={(mid or '?')[:8] if mid else '?'}")
        return mid
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        log.error(f"post_to_room failed {from_agent} -> {room_id}: HTTP {e.code} {body}")
        return None
    except Exception as e:
        log.error(f"post_to_room failed {from_agent} -> {room_id}: {e}")
        return None


def room_ctx(msg):
    """If this incoming msg came via a room (@-mention bridge), return its
    room_id; else None. Reads inbox message context {"via":"room","room_id":..}.
    """
    try:
        ctx = msg.get("context") or {}
        if isinstance(ctx, dict) and ctx.get("via") == "room":
            return ctx.get("room_id")
    except Exception:
        pass
    return None


def extract_real_sender(msg):
    text = msg.get("text", "")
    m = re.match(r"^\[from ([a-zA-Z0-9_\-]+)\]", text)
    if m:
        candidate = m.group(1)
        if candidate in AGENT_KEYS:
            return candidate
    return msg.get("from")


AUTO_REPLY_MARKER_RE = re.compile(r"(\[from \S+ server-runtime [^\]]+\]|— автоматический ответ помощника проекта\.)")


def match_trigger(text, triggers):
    if AUTO_REPLY_MARKER_RE.search(text):
        return {"pattern": "auto-reply-pingpong-guard", "priority": 0, "action": "silent"}
    for t in sorted(triggers, key=lambda x: x.get("priority", 99)):
        try:
            if re.search(t["pattern"], text):
                return t
        except re.error as e:
            log.warning(f"regex error: {e}")
            continue
    return None


def deepseek_reply_with_tools(persona, message):
    """Phase 2: DeepSeek function calling. Up to 3 tool iterations, then final reply."""
    api_key = DS_ENV.get("DEEPSEEK_API_KEY")
    base_url = DS_ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    if not api_key:
        return None

    llm = persona.get("llm_settings", {})
    sys_prompt = llm.get("system_prompt", "Reply briefly in Russian.")
    sys_prompt += (
        "\n\nYou have access to tools for FACTUAL information. "
        "ALWAYS call relevant tools first if user asks about current state of disk, memory, "
        "containers, logs, database, git, blackboard, etc. "
        "After collecting tool results, synthesize a concise answer in Russian. "
        "Cite tool output verbatim when stating facts. "
        "If no tool fits — reply briefly without inventing data."
    )

    user_msg = f"From {message.get('from', '?')}: {message.get('text', '')[:2000]}"
    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]

    tools_schema = get_tools_for_persona(persona)
    tool_calls_made = 0
    tool_results_log = []  # for finalize fallback

    for iteration in range(MAX_TOOL_CALLS_PER_REPLY + 1):
        payload = {
            "model": llm.get("model", "deepseek-chat"),
            "messages": msgs,
            "max_tokens": llm.get("max_tokens", 800),
            "temperature": llm.get("temperature", 0.3),
        }
        # Only offer tools while under budget
        if tools_schema and tool_calls_made < MAX_TOOL_CALLS_PER_REPLY:
            payload["tools"] = tools_schema
            payload["tool_choice"] = "auto"

        try:
            d = http_post(
                f"{base_url}/chat/completions",
                payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=60,
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300] if e.fp else ""
            log.error(f"deepseek HTTP {e.code}: {body}")
            # Schema error → break to finalize fallback
            break
        except Exception as e:
            log.error(f"deepseek call failed: {e}")
            break

        choice = d["choices"][0]
        msg = choice["message"]
        finish_reason = choice.get("finish_reason")

        if finish_reason == "tool_calls" and msg.get("tool_calls"):
            msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg["tool_calls"],
            })
            for tc in msg["tool_calls"][:MAX_TOOL_CALLS_PER_REPLY - tool_calls_made]:
                tool_calls_made += 1
                tname = tc["function"]["name"]
                try:
                    targs = json.loads(tc["function"]["arguments"] or "{}")
                except Exception:
                    targs = {}
                log.info(f"[{persona['persona_id']}] TOOL_CALL #{tool_calls_made} {tname}({targs})")
                result = execute_tool(tname, targs)
                tool_results_log.append(f"--- {tname}({targs}) ---\n{result[:1500]}")
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result[:1500],
                })
            continue

        # Got final reply
        content = (msg.get("content") or "").strip()
        if "DSML" in content or "<tool_call>" in content or "<invoke" in content:
            content = ""
        if content:
            return content
        # Empty content — break to finalize fallback
        break

    # Finalize fallback: clean call with tool results consolidated, NO tools schema
    if not tool_results_log:
        return "(no reply)"
    log.warning(f"[{persona['persona_id']}] finalize fallback (clean call without tools)")
    consolidated = "\n\n".join(tool_results_log)[:5000]
    finalize_msgs = [
        {"role": "system", "content": sys_prompt + "\n\nTool results below. Synthesize SHORT user-facing answer in Russian. NO tool syntax. NO function names. Just the answer."},
        {"role": "user", "content": user_msg},
        {"role": "user", "content": f"Tool results collected:\n\n{consolidated}\n\nNow write the final answer."},
    ]
    try:
        d = http_post(
            f"{base_url}/chat/completions",
            {
                "model": llm.get("model", "deepseek-chat"),
                "messages": finalize_msgs,
                "max_tokens": llm.get("max_tokens", 800),
                "temperature": 0.2,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        return (d["choices"][0]["message"].get("content") or "(empty finalize)").strip()
    except Exception as e:
        log.error(f"finalize call failed: {e}")
        return f"(tool data collected but finalize failed: {e})"


class HistoryStore:
    def __init__(self, persona_id):
        self.path = os.path.join(HISTORY_DIR, f"{persona_id}.json")
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except Exception:
            return {"seen_ids": [], "replies_per_hour": [], "by_parent": {}}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f)
        except Exception as e:
            log.warning(f"history save failed: {e}")

    def already_seen(self, msg_id):
        return msg_id in self.data["seen_ids"]

    def mark_seen(self, msg_id):
        self.data["seen_ids"].append(msg_id)
        self.data["seen_ids"] = self.data["seen_ids"][-200:]

    def can_reply(self, persona):
        rules = persona.get("escalation_rules", {})
        max_per_hour = rules.get("max_auto_replies_per_hour", 10)
        now = time.time()
        self.data["replies_per_hour"] = [t for t in self.data["replies_per_hour"] if now - t < 3600]
        if len(self.data["replies_per_hour"]) >= max_per_hour:
            return False, f"rate limit {len(self.data['replies_per_hour'])}/{max_per_hour} per hour"
        return True, ""

    def record_reply(self, msg_id, parent_id=None):
        self.data["replies_per_hour"].append(time.time())
        if parent_id:
            self.data["by_parent"].setdefault(parent_id, []).append(msg_id)
            if len(self.data["by_parent"]) > 50:
                items = sorted(self.data["by_parent"].items(), key=lambda x: x[0])[-50:]
                self.data["by_parent"] = dict(items)

    def loop_depth(self, parent_id, persona):
        rules = persona.get("escalation_rules", {})
        max_depth = rules.get("loop_max_depth", 2)
        if not parent_id:
            return 0, max_depth
        return len(self.data["by_parent"].get(parent_id, [])), max_depth


def handle_silent(persona, msg, history):
    log.info(f"[{persona['persona_id']}] SILENT msg={msg.get('id', '?')[:8]}")
    return None


def handle_auto_ack(persona, msg, history):
    template = persona.get("auto_ack_template", "Acknowledged.")
    msg_id_short = (msg.get("id") or "?")[:8]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    text = template.replace("{message_id_short}", msg_id_short).replace("{ts}", ts)
    sender = extract_real_sender(msg)
    if not sender:
        return None
    # Reverse room bridge: room-originated message -> ack in the room, not in DM.
    room_id = room_ctx(msg)
    if room_id:
        sent_id = post_to_room(room_id, persona["persona_id"], text)
        if sent_id:
            log.info(f"[{persona['persona_id']}] AUTO_ACK(room) -> {room_id} reply={sent_id[:8]}")
        return sent_id
    sent_id = send_dm(persona["persona_id"], sender, text, parent_id=msg.get("id"))
    if sent_id:
        log.info(f"[{persona['persona_id']}] AUTO_ACK to {sender} reply={sent_id[:8]}")
    return sent_id


def handle_escalate(persona, msg, history):
    sender = extract_real_sender(msg) or "?"
    snippet = (msg.get("text", "")[:300]).replace("\n", " ")
    if os.path.exists(NOTIFY_BIN):
        try:
            subprocess.run(
                [NOTIFY_BIN, f"URGENT DM to {persona['persona_id']} from {sender}: {snippet}"],
                timeout=10, check=False,
            )
        except Exception as e:
            log.warning(f"notify failed: {e}")
    log.info(f"[{persona['persona_id']}] ESCALATE from {sender}")
    return None


def handle_llm_reply(persona, msg, history):
    sender = extract_real_sender(msg)
    if not sender:
        return None
    reply_text = deepseek_reply_with_tools(persona, msg)
    if not reply_text:
        log.warning(f"[{persona['persona_id']}] llm_reply empty fallback to auto_ack")
        return handle_auto_ack(persona, msg, history)
    # The owner wants the AGENT's OWN voice — no "— автоматический ответ помощника
    # проекта" marker. Anti-loop is preserved without it: the forward-bridge only
    # bridges @-addressed messages (this reply has no @mention -> never re-bridged),
    # plus loop_depth/can_reply caps any chain. Reverse room bridge: a question that
    # arrived via a room @-mention is answered IN THE ROOM.
    room_id = room_ctx(msg)
    if room_id:
        sent_id = post_to_room(room_id, persona["persona_id"], reply_text)
        log.info(f"[{persona['persona_id']}] LLM_REPLY(room) -> {room_id} ({len(reply_text)} chars) reply={sent_id[:8] if sent_id else '?'}")
        return sent_id
    sent_id = send_dm(persona["persona_id"], sender, reply_text, parent_id=msg.get("id"))
    log.info(f"[{persona['persona_id']}] LLM_REPLY to {sender} ({len(reply_text)} chars) reply={sent_id[:8] if sent_id else '?'}")
    return sent_id


ROUTINE_BETA_HEADER = "experimental-cc-routine-2026-04-01"


def handle_cloud_routine(persona, msg, history):
    """Wake a REAL cloud Claude via the agent's Routine API trigger (POST /fire).
    The cloud session is expected to post its reply BACK into the room via the
    Cognitive Core MCP connector (room_id is passed in the fired text). Returns the
    cloud session id on success, or None to signal fallback to the DeepSeek persona
    (e.g. no token configured, or the fire call failed)."""
    pid = persona["persona_id"]
    cfg = persona.get("channel_config") or {}
    fire_url = cfg.get("fire_url") or cfg.get("url")
    token = cfg.get("token")
    if not fire_url or not token:
        log.warning(f"[{pid}] claude_routine: no fire_url/token -> fallback deepseek")
        return None
    room_id = room_ctx(msg)
    sender = extract_real_sender(msg) or "owner"
    text = msg.get("text", "")
    fired_text = (
        f"Тебе ({pid}) написал {sender} в комнате Cognitive Core (room_id={room_id}). "
        f"Ответь по делу на русском. Нужен контекст про дела владельца — вызови "
        f"cognitive_recall. ОБЯЗАТЕЛЬНО отправь ответ обратно в эту комнату через "
        f"room_post с room_id={room_id}. Текст сообщения:\n\n{text}"
    )
    try:
        body = json.dumps({"text": fired_text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(fire_url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", ROUTINE_BETA_HEADER)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode())
        sid = d.get("claude_code_session_id") or d.get("session_id") or "fired"
        log.info(f"[{pid}] CLOUD_ROUTINE fired -> session={sid} room={room_id}")
        return sid
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:200] if e.fp else ""
        log.error(f"[{pid}] claude_routine fire HTTP {e.code} {b} -> fallback deepseek")
        return None
    except Exception as e:
        log.error(f"[{pid}] claude_routine fire failed: {e} -> fallback deepseek")
        return None


def handle_managed(persona, msg, history):
    """Channel 'managed': answer with the REAL Claude via the Anthropic Messages API
    (owner's sk-ant key from config) and post the reply back into the room — the
    daemon mediates the round-trip, so no MCP connector is required. Per DeepSeek
    this practical 'Claude API direct' fits the rooms use-case better than full
    Managed Agents (sessions/agent/environment): same real Claude, far less owner
    setup. config: {api_key, model?}. Returns the reply id, or None to fall back
    to the DeepSeek persona (no key / API error / empty reply)."""
    pid = persona["persona_id"]
    cfg = persona.get("channel_config") or {}
    api_key = cfg.get("api_key") or cfg.get("key")
    if not api_key:
        log.warning(f"[{pid}] managed: no api_key -> fallback deepseek")
        return None
    model = cfg.get("model", "claude-3-5-sonnet-20241022")
    sender = extract_real_sender(msg) or "owner"
    text = msg.get("text", "")
    sys_prompt = (persona.get("llm_settings") or {}).get(
        "system_prompt", "Ты ассистент владельца. Ответь кратко и по делу на русском.")
    try:
        body = json.dumps({
            "model": model,
            "max_tokens": 1024,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=40) as resp:
            d = json.loads(resp.read().decode())
        parts = d.get("content") or []
        reply = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not reply:
            log.warning(f"[{pid}] managed: empty reply -> fallback deepseek")
            return None
        # No auto-reply marker — the agent answers in its own voice (anti-loop via
        # @-mention-only bridging + loop_depth).
        room_id = room_ctx(msg)
        if room_id:
            sid = post_to_room(room_id, pid, reply)
            log.info(f"[{pid}] MANAGED(room) -> {room_id} ({len(reply)} chars) reply={sid[:8] if sid else '?'}")
            return sid
        sid = send_dm(pid, sender, reply, parent_id=msg.get("id"))
        log.info(f"[{pid}] MANAGED(dm) -> {sender} reply={sid[:8] if sid else '?'}")
        return sid
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:200] if e.fp else ""
        log.error(f"[{pid}] managed Claude API HTTP {e.code} {b} -> fallback deepseek")
        return None
    except Exception as e:
        log.error(f"[{pid}] managed failed: {e} -> fallback deepseek")
        return None


def handle_custom_llm(persona, msg, history):
    """Channel 'custom_llm': answer via ANY OpenAI-compatible provider the owner
    configured (config {base_url, api_key, model}) — POST {base_url}/chat/completions.
    Covers OpenAI / DeepSeek / Mistral / Groq / OpenRouter / Ollama / etc. The daemon
    posts the reply back to the room. Returns reply id, or None -> fallback to the
    built-in DeepSeek persona (no config / API error / empty reply)."""
    pid = persona["persona_id"]
    cfg = persona.get("channel_config") or {}
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or cfg.get("key")
    model = cfg.get("model")
    if not base_url or not model:
        log.warning(f"[{pid}] custom_llm: no base_url/model -> fallback deepseek")
        return None
    url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    text = msg.get("text", "")
    sender = extract_real_sender(msg) or "owner"
    sys_prompt = (persona.get("llm_settings") or {}).get(
        "system_prompt", "Ты ассистент владельца. Ответь кратко и по делу на русском.")
    try:
        body = json.dumps({
            "model": model,
            "max_tokens": 800,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=45) as resp:
            d = json.loads(resp.read().decode())
        reply = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not reply:
            log.warning(f"[{pid}] custom_llm: empty reply -> fallback deepseek")
            return None
        # No auto-reply marker — agent answers in its own voice.
        room_id = room_ctx(msg)
        if room_id:
            sid = post_to_room(room_id, pid, reply)
            log.info(f"[{pid}] CUSTOM_LLM(room) -> {room_id} model={model} ({len(reply)}ch) reply={sid[:8] if sid else '?'}")
            return sid
        sid = send_dm(pid, sender, reply, parent_id=msg.get("id"))
        log.info(f"[{pid}] CUSTOM_LLM(dm) -> {sender} model={model} reply={sid[:8] if sid else '?'}")
        return sid
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:200] if e.fp else ""
        log.error(f"[{pid}] custom_llm HTTP {e.code} {b} -> fallback deepseek")
        return None
    except Exception as e:
        log.error(f"[{pid}] custom_llm failed: {e} -> fallback deepseek")
        return None


def handle_webhook(persona, msg, history):
    """Channel 'webhook' — the 'wake-me' mode (vs the 'answer-for-me' modes above).
    The single central daemon WAKES the real agent by POSTing the room event to the
    agent's own webhook (config {webhook_url, secret?}); the agent then responds and
    posts back to the room itself. One central waker for ALL agents — no per-agent
    poller. Returns a success sentinel (so the daemon does NOT also stand-in reply),
    or None to fall back to DeepSeek (no webhook configured / POST failed)."""
    pid = persona["persona_id"]
    cfg = persona.get("channel_config") or {}
    hook = cfg.get("webhook_url") or cfg.get("url")
    if not hook:
        log.warning(f"[{pid}] webhook: no webhook_url -> fallback deepseek")
        return None
    room_id = room_ctx(msg)
    sender = extract_real_sender(msg) or "owner"
    payload = {
        "event": "room_message",
        "agent_id": pid,
        "room_id": room_id,
        "from": sender,
        "text": msg.get("text", ""),
    }
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(hook, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        secret = cfg.get("secret")
        if secret:
            req.add_header("X-Wake-Secret", secret)
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.getcode()
        log.info(f"[{pid}] WEBHOOK woke agent -> {hook} HTTP {code} room={room_id}")
        return f"webhook:{code}"  # success — the agent posts its own reply to the room
    except urllib.error.HTTPError as e:
        log.error(f"[{pid}] webhook HTTP {e.code} -> fallback deepseek")
        return None
    except Exception as e:
        log.error(f"[{pid}] webhook failed: {e} -> fallback deepseek")
        return None


ACTION_HANDLERS = {
    "silent": handle_silent,
    "auto_ack": handle_auto_ack,
    "escalate": handle_escalate,
    "llm_reply": handle_llm_reply,
}


def process_persona(persona):
    pid = persona["persona_id"]
    history = HistoryStore(pid)
    msgs = load_inbox(pid, since_minutes=60)
    new_count = 0
    for msg in msgs:
        msg_id = msg.get("id")
        if not msg_id:
            continue
        if history.already_seen(msg_id):
            continue
        real_sender = extract_real_sender(msg)
        if real_sender == pid:
            history.mark_seen(msg_id)
            continue
        history.mark_seen(msg_id)
        new_count += 1
        text = msg.get("text", "")
        trigger = match_trigger(text, persona.get("triggers", []))
        if not trigger:
            continue
        action = trigger.get("action", "silent")
        log.info(f"[{pid}] msg={msg_id[:8]} from={msg.get('from', '?')} action={action} prio={trigger.get('priority')}")
        if action in ("auto_ack", "llm_reply"):
            ok, reason = history.can_reply(persona)
            if not ok:
                log.warning(f"[{pid}] BLOCKED: {reason}")
                continue
            depth, max_depth = history.loop_depth(msg_id, persona)
            if depth >= max_depth:
                log.warning(f"[{pid}] LOOP_BLOCKED depth={depth}>={max_depth}")
                continue
        channel = persona.get("wake_channel", "deepseek")
        if action == "llm_reply" and channel == "claude_routine":
            # Route to the REAL cloud Claude via Routine /fire; if it can't fire
            # (no token / error) fall back to the DeepSeek persona so the owner
            # still gets an answer. No dup: the cloud posts back as the agent, which
            # the daemon skips as a self-message.
            sent_id = handle_cloud_routine(persona, msg, history)
            if sent_id is None:
                sent_id = handle_llm_reply(persona, msg, history)
        elif action == "llm_reply" and channel == "managed":
            # Real Claude via Anthropic Messages API; fall back to DeepSeek if no
            # key / API error so the owner still gets an answer.
            sent_id = handle_managed(persona, msg, history)
            if sent_id is None:
                sent_id = handle_llm_reply(persona, msg, history)
        elif action == "llm_reply" and channel == "custom_llm":
            # Any OpenAI-compatible provider the owner configured; DeepSeek fallback.
            sent_id = handle_custom_llm(persona, msg, history)
            if sent_id is None:
                sent_id = handle_llm_reply(persona, msg, history)
        elif action == "llm_reply" and channel == "webhook":
            # 'Wake-me' mode: wake the real agent via its webhook; it answers itself.
            # DeepSeek fallback only if the webhook isn't set / POST fails.
            sent_id = handle_webhook(persona, msg, history)
            if sent_id is None:
                sent_id = handle_llm_reply(persona, msg, history)
        else:
            handler = ACTION_HANDLERS.get(action, handle_silent)
            sent_id = handler(persona, msg, history)
        if action in ("auto_ack", "llm_reply") and sent_id:
            history.record_reply(sent_id, parent_id=msg_id)
    if new_count:
        log.info(f"[{pid}] processed {new_count} new msgs")
    history.save()


def main():
    log.info(f"=== Cognitive Agent Runtime v2 starting (tools: {sorted(TOOL_REGISTRY.keys())}) ===")
    last_persona_load = 0
    last_sig = None
    personas = {}
    while True:
        try:
            now = time.time()
            if now - last_persona_load > PERSONA_REFRESH_SEC:
                custom = load_personas()
                standin = load_standin_agents()
                # Opt-in: serve ONLY agents with standin_enabled=true. Use their
                # custom agent_persona if defined, else a context-aware default.
                personas = {
                    aid: (custom.get(aid) or default_persona(aid, label))
                    for aid, label in standin.items()
                }
                # Attach per-agent connection channel + (secret) config so the
                # dispatcher in process_persona can route deepseek/claude_routine/managed.
                for _aid, _p in personas.items():
                    _p["wake_channel"], _p["channel_config"] = load_agent_channel(_aid)
                n_custom = sum(1 for a in personas if a in custom)
                # Log only when the (agent, channel) set actually changes — avoids
                # a log line every refresh now that refresh is frequent (60s).
                sig = sorted((a, p.get("wake_channel", "deepseek")) for a, p in personas.items())
                if sig != last_sig:
                    log.info(
                        f"loaded {len(personas)} stand-in personas "
                        f"(custom={n_custom}, default={len(personas) - n_custom}): {sig}")
                    last_sig = sig
                last_persona_load = now
            if not personas:
                log.warning("no personas active sleeping")
                time.sleep(60)
                continue
            for pid, persona in personas.items():
                try:
                    process_persona(persona)
                except Exception as e:
                    log.error(f"[{pid}] process failed: {e}")
            poll_sec = min(p.get("poll_interval_seconds", DEFAULT_POLL_SEC) for p in personas.values())
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            log.info("keyboard interrupt shutting down")
            break
        except Exception as e:
            log.error(f"main loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
