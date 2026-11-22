"""Isolated DBOS executor for explicitly enqueued, read-only workspace snapshots.

Core is the business ledger. DBOS stores only technical execution history.
No credential or client/config object is passed as a durable workflow argument.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from tools.coordination_client import CoordinationClient, CoordinationError

APPLICATION_NAME = "cogcore-coordination"
APPLICATION_VERSION = "workspace-snapshot-v1"
WORKFLOW_NAME = "cogcore.workspace_snapshot.v1"
HANDLER = "workspace_snapshot"


class FencedAttempt(RuntimeError):
    """The authoritative ledger no longer authorizes this attempt."""


class ArtifactConflict(RuntimeError):
    """An existing artifact cannot safely be attributed to these inputs."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class WorkerSettings:
    agent_id: str
    session_id: str
    executor_id: str
    allowed_roots: tuple[Path, ...]
    artifacts_dir: Path
    database_url: str = field(repr=False)
    application_name: str = APPLICATION_NAME
    application_version: str = APPLICATION_VERSION
    concurrency: int = 1
    lease_seconds: int = 300
    reconcile_seconds: float = 15
    project: str | None = None

    @property
    def queue_name(self) -> str:
        # A stable worker-specific queue avoids another host claiming a local-path job.
        return "coord-snapshot-" + digest({"agent": self.agent_id, "session": self.session_id})[:24]

    @classmethod
    def from_env(cls) -> WorkerSettings:
        def required(key: str) -> str:
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"{key} is required")
            return value

        roots = json.loads(required("COGCORE_COORD_ALLOWED_ROOTS"))
        if not isinstance(roots, list) or not roots or not all(isinstance(x, str) for x in roots):
            raise ValueError("COGCORE_COORD_ALLOWED_ROOTS must be a nonempty JSON array of absolute directories")
        paths = [Path(x) for x in roots]
        artifacts = Path(required("COGCORE_COORD_ARTIFACTS_DIR"))
        if any(not p.is_absolute() or not p.is_dir() for p in [*paths, artifacts]):
            raise ValueError("Allowed roots and artifacts directory must already exist and be absolute")
        database_url = required("COGCORE_COORD_DB_URL")
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("COGCORE_COORD_DB_URL must use a dedicated PostgreSQL database")
        concurrency = int(os.environ.get("COGCORE_COORD_CONCURRENCY", "1"))
        lease = int(os.environ.get("COGCORE_COORD_LEASE_SECONDS", "300"))
        interval = float(os.environ.get("COGCORE_COORD_RECONCILE_SECONDS", "15"))
        if not 1 <= concurrency <= 8 or not 120 <= lease <= 3600 or not 1 <= interval <= 300:
            raise ValueError("concurrency must be 1..8, lease 120..3600, reconciliation interval 1..300")
        return cls(agent_id=required("COGCORE_COORD_AGENT_ID"), session_id=required("COGCORE_COORD_SESSION_ID"),
                   executor_id=required("COGCORE_COORD_EXECUTOR_ID"), allowed_roots=tuple(p.resolve() for p in paths),
                   artifacts_dir=artifacts.resolve(), database_url=database_url,
                   application_name=os.environ.get("COGCORE_COORD_APP_NAME", APPLICATION_NAME),
                   application_version=os.environ.get("COGCORE_COORD_APP_VERSION", APPLICATION_VERSION),
                   concurrency=concurrency, lease_seconds=lease, reconcile_seconds=interval,
                   project=os.environ.get("COGCORE_COORD_PROJECT") or None)


def workflow_id(task_id: str, attempt_id: str) -> str:
    return f"coord:{UUID(task_id)}:{UUID(attempt_id)}"


def input_fingerprint(task: dict) -> str:
    return digest({"schema_version": 1, "handler": HANDLER, "task_id": task["task_id"],
                   "worktree": task.get("worktree"), "project": task.get("project"),
                   "acceptance": task.get("acceptance"), "create_hash": task.get("create_hash")})


def allowed_worktree(task: dict, settings: WorkerSettings) -> Path:
    raw = task.get("worktree")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("Task requires an explicit absolute worktree")
    path = Path(raw).resolve(strict=True)
    if not path.is_dir() or not any(path.is_relative_to(root) for root in settings.allowed_roots):
        raise ValueError("Task worktree is outside the configured allowlist")
    return path


def artifact_path(settings: WorkerSettings, task_id: str, attempt_id: str) -> Path:
    return settings.artifacts_dir / f"{UUID(task_id)}.{UUID(attempt_id)}.json"


def read_artifact(path: Path, task_id: str, attempt_id: str, fingerprint: str) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ArtifactConflict("Artifact path is not a regular file in the configured directory")
    if path.stat().st_size > 8_000_000:
        raise ArtifactConflict("Existing artifact is too large")
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
        content_hash = artifact["content_sha256"]
        content = {k: v for k, v in artifact.items() if k != "content_sha256"}
        valid = (artifact["schema_version"] == 1 and artifact["handler"] == HANDLER
                 and artifact["task_id"] == task_id and artifact["attempt_id"] == attempt_id
                 and artifact["input_fingerprint"] == fingerprint and content_hash == digest(content)
                 and isinstance(artifact["git"], dict) and bool(artifact["observed_at"]))
        if not valid:
            raise ValueError("mismatch")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ArtifactConflict("Existing artifact identity, input fingerprint or content hash mismatched") from exc
    return artifact


def _git(worktree: Path, arguments: list[str], *, extra_config: list[str] | None = None,
         acceptable: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    # No shell and no optional index locks. Disable repo-configured process hooks.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               *(extra_config or []), "-C", str(worktree), *arguments]
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Read-only git inspection failed or timed out") from None
    if result.returncode not in acceptable:
        raise ValueError("Read-only git inspection rejected this worktree")
    return result


def git_snapshot(worktree: Path) -> dict:
    top = _git(worktree, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8").strip()
    if Path(top).resolve() != worktree:
        raise ValueError("Task worktree must be the exact Git worktree root")
    # git status may run clean/process filters when hashing a changed file. Neutralize
    # every configured filter, including config includes, before examining content.
    names = _git(worktree, ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                 acceptable=(0, 1)).stdout.decode("utf-8").split("\0")
    config = []
    for name in names:
        if name:
            if not re.fullmatch(r"filter\.[^\r\n\x00=]+\.(clean|smudge|process|required)", name):
                raise ValueError("Unsupported Git filter configuration")
            config.extend(["-c", name + ("=false" if name.endswith(".required") else "=")])
    head = _git(worktree, ["rev-parse", "--verify", "HEAD"], extra_config=config).stdout.decode("ascii").strip()
    branch = _git(worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"], extra_config=config,
                  acceptable=(0, 1)).stdout.decode("utf-8", errors="backslashreplace").strip() or None
    status = _git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all"],
                  extra_config=config).stdout.decode("utf-8", errors="backslashreplace")
    if len(status.encode("utf-8")) > 4_000_000:
        raise ValueError("Git status exceeds the bounded snapshot size")
    return {"branch": branch, "sha": head, "status_porcelain_v1_z": status, "dirty": bool(status),
            "submodules_inspected": False}


def publish_artifact(path: Path, artifact: dict) -> None:
    """Publish a complete, fsynced file atomically without overwriting an earlier result."""
    data = canonical(artifact) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".coord-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Same filesystem: an atomic, exclusive publication, unlike replace().
            os.link(temporary, path)
        except FileExistsError:
            pass  # Caller validates the winner, never trusts it implicitly.
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def assert_identity(task: dict, settings: WorkerSettings, attempt_id: str, fingerprint: str) -> None:
    if (task.get("worker") != {"agent_id": settings.agent_id, "session_id": settings.session_id}
            or task.get("attempt_id") != attempt_id or input_fingerprint(task) != fingerprint):
        raise FencedAttempt("Attempt, worker session or immutable task inputs changed")


def fresh_running(client: CoordinationClient, settings: WorkerSettings, task_id: str,
                  attempt_id: str, fingerprint: str) -> dict:
    task = client.get(task_id)
    assert_identity(task, settings, attempt_id, fingerprint)
    if task["status"] != "running":
        raise FencedAttempt("Core task is no longer running (cancelled, replaced or awaiting review)")
    return task


def renew(client: CoordinationClient, settings: WorkerSettings, task_id: str, attempt_id: str,
          fingerprint: str, reconciliation: str) -> dict:
    task = fresh_running(client, settings, task_id, attempt_id, fingerprint)
    action = "recover" if task.get("lease_expired") else "heartbeat"
    client.action(task_id, {"action": action, "session_id": settings.session_id,
                           "attempt_id": attempt_id, "expected_version": task["version"],
                           "idempotency_key": f"coord:{action}:{attempt_id}:{task['version']}",
                           "lease_seconds": settings.lease_seconds, "reconciliation": reconciliation})
    # A replayed HTTP idempotency receipt is historical, not a current ownership proof.
    current = fresh_running(client, settings, task_id, attempt_id, fingerprint)
    if current.get("lease_expired"):
        raise FencedAttempt("Lease is expired after renewal")
    return current


def _evidence(path: Path) -> dict:
    return {"uri": path.as_uri(), "description": "Read-only Git workspace snapshot; independent verification required",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _submission(task_id: str, attempt_id: str, evidence: dict) -> dict:
    return {"task_id": task_id, "attempt_id": attempt_id, "submission_status": "review", "evidence": evidence}


def _test_crash(settings: WorkerSettings, stage: str) -> None:
    if os.environ.get("COGCORE_COORD_TEST_CRASH") != stage:
        return
    # Fault injection can never be accidentally enabled against a remote Core or
    # an ordinary DB name. It is reserved for the explicit isolated test harness.
    api_host = urlsplit(os.environ.get("COGCORE_COORD_API_URL", "")).hostname
    database = urlsplit(settings.database_url).path.rsplit("/", 1)[-1]
    if os.environ.get("COGCORE_COORD_TEST_MODE") != "1" or api_host not in {"127.0.0.1", "localhost", "::1"} or not database.startswith("test_"):
        raise ValueError("Crash injection requires test mode, loopback Core and a test_ PostgreSQL database")
    os._exit(86)


def run_attempt(client: CoordinationClient, settings: WorkerSettings, task_id: str,
                attempt_id: str, session_id: str, fingerprint: str) -> dict:
    """At-least-once step. Every invocation rechecks Core; no cached claim is trusted."""
    if session_id != settings.session_id:
        raise FencedAttempt("Workflow belongs to a different worker session")
    task = client.get(task_id)
    assert_identity(task, settings, attempt_id, fingerprint)
    worktree = allowed_worktree(task, settings)
    path = artifact_path(settings, task_id, attempt_id)
    artifact = read_artifact(path, task_id, attempt_id, fingerprint)
    if task["status"] in {"review", "verified_done"} and artifact is not None:
        # A crash after Core committed submit, before DBOS checkpointed its response.
        evidence = _evidence(path)
        if evidence in task.get("evidence", []):
            return _submission(task_id, attempt_id, evidence)
        raise FencedAttempt("Existing submission does not match this artifact")
    reconciliation = ("Verified existing atomic artifact " + artifact["content_sha256"] if artifact else
                      "No published artifact for this attempt; only the bounded read-only snapshot may run")
    renew(client, settings, task_id, attempt_id, fingerprint, reconciliation)
    if artifact is None:
        artifact = {"schema_version": 1, "handler": HANDLER, "task_id": task_id, "attempt_id": attempt_id,
                    "input_fingerprint": fingerprint, "worktree": str(worktree),
                    "observed_at": datetime.now(timezone.utc).isoformat(), "git": git_snapshot(worktree)}
        artifact["content_sha256"] = digest(artifact)
        renew(client, settings, task_id, attempt_id, fingerprint, "Read-only Git inspection finished; no artifact published yet")
        publish_artifact(path, artifact)
        _test_crash(settings, "after_publish")
        artifact = read_artifact(path, task_id, attempt_id, fingerprint)
        if artifact is None:
            raise ArtifactConflict("Atomic artifact disappeared before submission")
    evidence = _evidence(path)
    task = renew(client, settings, task_id, attempt_id, fingerprint,
                 "Verified atomic artifact " + artifact["content_sha256"] + "; ready for independent review")
    submitted = client.action(task_id, {"action": "submit", "session_id": settings.session_id,
                                      "attempt_id": attempt_id, "expected_version": task["version"],
                                      "idempotency_key": f"coord:submit:{attempt_id}:{task['version']}",
                                      "detail": "workspace_snapshot artifact recorded; independent review required",
                                      "next_step": "An independent session verifies the artifact, worktree and acceptance criteria",
                                      "evidence": [evidence]})
    assert_identity(submitted, settings, attempt_id, fingerprint)
    if submitted["status"] != "review" or evidence not in submitted.get("evidence", []):
        raise FencedAttempt("Core did not acknowledge the expected submission")
    return _submission(task_id, attempt_id, evidence)


def block_failed_attempt(client: CoordinationClient, settings: WorkerSettings, task_id: str,
                         attempt_id: str, session_id: str, fingerprint: str) -> None:
    if session_id != settings.session_id:
        raise FencedAttempt("Workflow session changed")
    task = renew(client, settings, task_id, attempt_id, fingerprint,
                 "Technical handler failed; no automatic retry or acceptance. Inspect attempt artifact before coordinator retry")
    receipt = client.action(task_id, {"action": "block", "session_id": session_id, "attempt_id": attempt_id,
                                    "expected_version": task["version"], "idempotency_key": f"coord:block:{attempt_id}:{task['version']}",
                                    "detail": "Durable snapshot handler failed; inspect DBOS execution and attempt artifact",
                                    "next_step": "Coordinator reconciles existing effects before retrying the task"})
    if receipt["status"] != "blocked":
        raise FencedAttempt("Core did not acknowledge blocking the failed attempt")


def _dbos_client(settings: WorkerSettings):
    from dbos import DBOSClient, DBOSPortableJSONSerializer
    return DBOSClient(system_database_url=settings.database_url, application_name=settings.application_name,
                      serializer=DBOSPortableJSONSerializer(), retry_connection_errors=False)


def enqueue_task(client: CoordinationClient, durable_client, settings: WorkerSettings, task_id: str) -> dict:
    task_id = str(UUID(task_id))
    task = client.get(task_id)
    if task.get("reference_acceptance"):
        raise ValueError("Git snapshot worker cannot inspect a reference deliverable")
    allowed_worktree(task, settings)  # Reject unsafe/unconfigured input before taking ownership.
    fingerprint = input_fingerprint(task)
    if task["status"] == "queued":
        client.action(task_id, {"action": "claim", "session_id": settings.session_id,
                               "expected_version": task["version"], "lease_seconds": settings.lease_seconds,
                               "idempotency_key": "coord:claim:" + digest({"task": task_id, "version": task["version"],
                                                                         "agent": settings.agent_id, "session": settings.session_id})})
        _test_crash(settings, "after_claim")
    # Read after claim even when HTTP returned an idempotent, cached old result.
    task = client.get(task_id)
    attempt_id = task.get("attempt_id")
    if not attempt_id:
        raise FencedAttempt("Core did not assign an attempt")
    assert_identity(task, settings, attempt_id, fingerprint)
    if task["status"] != "running":
        raise FencedAttempt("Only a current running attempt can be enqueued")
    identifier = workflow_id(task_id, attempt_id)
    durable_client.enqueue({"workflow_name": WORKFLOW_NAME, "queue_name": settings.queue_name,
                            "workflow_id": identifier, "app_version": settings.application_version,
                            "application_name": settings.application_name},
                           task_id, attempt_id, settings.session_id, fingerprint)
    return {"workflow_id": identifier, "task_id": task_id, "attempt_id": attempt_id, "status": "enqueued"}


def reconcile_running(client: CoordinationClient, durable_client, settings: WorkerSettings) -> int:
    count = 0
    for task in client.iter_tasks(project=settings.project, status="running"):
        if task.get("worker") != {"agent_id": settings.agent_id, "session_id": settings.session_id}:
            continue
        try:
            enqueue_task(client, durable_client, settings, task["task_id"])
            count += 1
        except (CoordinationError, FencedAttempt, ValueError, OSError) as exc:
            print(json.dumps({"event": "reconciliation_rejected", "task_id": task["task_id"],
                              "error_type": type(exc).__name__}), file=sys.stderr, flush=True)
    return count


def start(settings: WorkerSettings) -> None:
    from dbos import DBOS, DBOSPortableJSONSerializer

    DBOS(config={"name": settings.application_name, "application_version": settings.application_version,
                 "executor_id": settings.executor_id, "system_database_url": settings.database_url,
                 "enable_otlp": False, "serializer": DBOSPortableJSONSerializer(),
                 "max_executor_threads": max(4, settings.concurrency + 2)})

    @DBOS.step(name="cogcore.snapshot_step.v1", retries_allowed=True, max_attempts=5, interval_seconds=1,
               should_retry=lambda error: isinstance(error, CoordinationError) and error.retryable)
    def snapshot_step(task_id: str, attempt_id: str, session_id: str, fingerprint: str) -> dict:
        return run_attempt(CoordinationClient.from_env(), settings, task_id, attempt_id, session_id, fingerprint)

    @DBOS.step(name="cogcore.block_snapshot.v1")
    def failure_step(task_id: str, attempt_id: str, session_id: str, fingerprint: str) -> None:
        block_failed_attempt(CoordinationClient.from_env(), settings, task_id, attempt_id, session_id, fingerprint)

    @DBOS.workflow(name=WORKFLOW_NAME)
    def workspace_snapshot(task_id: str, attempt_id: str, session_id: str, fingerprint: str) -> dict:
        try:
            return snapshot_step(task_id, attempt_id, session_id, fingerprint)
        except Exception:
            # A rejected mutation never becomes a successful workflow result.
            failure_step(task_id, attempt_id, session_id, fingerprint)
            raise

    DBOS.listen_queues([settings.queue_name])
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signal, _frame: stop.set())
    durable_client = None
    try:
        DBOS.launch()
        DBOS.register_queue(settings.queue_name, worker_concurrency=settings.concurrency,
                            global_concurrency=settings.concurrency, polling_interval_sec=0.5)
        durable_client = _dbos_client(settings)
        client = CoordinationClient.from_env()
        print(json.dumps({"event": "worker_ready", "application": settings.application_name,
                          "version": settings.application_version, "executor": settings.executor_id,
                          "session": settings.session_id, "queue": settings.queue_name}), flush=True)
        while not stop.is_set():
            try:
                reconcile_running(client, durable_client, settings)
            except CoordinationError as exc:
                print(json.dumps({"event": "core_unavailable", "http_status": exc.status}), file=sys.stderr, flush=True)
            stop.wait(settings.reconcile_seconds)
    finally:
        if durable_client is not None:
            durable_client.destroy()
        DBOS.destroy(workflow_completion_timeout_sec=30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="Execute this worker's queue and reconcile stranded running attempts")
    enqueue = commands.add_parser("enqueue", help="Explicitly claim and enqueue one Core task")
    enqueue.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        if importlib.metadata.version("dbos") != "3.5.0":
            raise ValueError("This worker requires dbos==3.5.0 in an isolated environment")
        settings = WorkerSettings.from_env()
        if args.command == "start":
            start(settings)
        else:
            client = CoordinationClient.from_env()
            durable_client = _dbos_client(settings)
            try:
                print(json.dumps(enqueue_task(client, durable_client, settings, args.task_id), ensure_ascii=False))
            finally:
                durable_client.destroy()
        return 0
    except (ValueError, OSError, CoordinationError, FencedAttempt, importlib.metadata.PackageNotFoundError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        # Third-party exception messages may contain a database connection string.
        print(json.dumps({"error": "DBOS initialization or execution failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
