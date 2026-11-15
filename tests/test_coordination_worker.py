"""Pure safety tests plus opt-in real DBOS/PostgreSQL process crash recovery."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from tools import coordination_client as cli
from tools import coordination_worker as worker


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "worktree"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "README.txt")
    git(repo, "-c", "user.name=Coordination Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    settings = worker.WorkerSettings("test-worker", "test-session", "test-executor", (repo,), artifacts,
                                     "postgresql://localhost/test_unused")
    return repo, artifacts, settings


class Ledger:
    """A loopback protocol fixture, not a substitute for Core's PostgreSQL tests."""

    def __init__(self, repo, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.task = {"task_id": str(uuid4()), "status": "queued", "version": 0, "worker": None,
                     "attempt_id": None, "attempt_no": 0, "worktree": str(repo), "project": "isolated_test",
                     "acceptance": "Independent snapshot verification", "create_hash": "test-create",
                     "lease_until": None, "lease_expired": False, "evidence": []}
        self.receipts = {}
        self.actions = []
        self.reject_submit = False
        self.lose_submit_response = False

    def get(self, task_id):
        with self.lock:
            assert task_id == self.task["task_id"]
            task = copy.deepcopy(self.task)
            task["lease_expired"] = bool(task["status"] == "running" and task["lease_until"]
                                         and datetime.fromisoformat(task["lease_until"]) <= datetime.now(timezone.utc))
            return task

    def iter_tasks(self, **kwargs):
        task = self.get(self.task["task_id"])
        if kwargs.get("status") in (None, task["status"]):
            yield task

    def action(self, task_id, body):
        with self.lock:
            key = body["idempotency_key"]
            if key in self.receipts:
                original, result = self.receipts[key]
                if original != body:
                    raise cli.CoordinationError("idempotency_conflict", 409)
                return copy.deepcopy(result)
            task = self.get(task_id)
            if body["expected_version"] != task["version"]:
                raise cli.CoordinationError("stale_version", 409)
            action = body["action"]
            if action == "claim":
                if task["status"] != "queued":
                    raise cli.CoordinationError("not_claimable", 409)
                task.update(status="running", attempt_id=str(uuid4()), attempt_no=task["attempt_no"] + 1,
                            worker={"agent_id": self.settings.agent_id, "session_id": body["session_id"]})
            elif (body["session_id"] != task["worker"]["session_id"] or body.get("attempt_id") != task["attempt_id"]
                  or task["status"] != "running" or (task["lease_expired"] and action != "recover")):
                raise cli.CoordinationError("stale_attempt", 409)
            if action in {"claim", "heartbeat", "recover"}:
                if action == "recover" and not body.get("reconciliation"):
                    raise cli.CoordinationError("reconciliation_required", 422)
                task["lease_until"] = (datetime.now(timezone.utc) + timedelta(seconds=body["lease_seconds"])).isoformat()
                task["lease_expired"] = False
            elif action == "submit":
                if self.reject_submit:
                    raise cli.CoordinationError("submit_forbidden", 403)
                task.update(status="review", evidence=body["evidence"], result=body["detail"], lease_until=None)
            elif action == "block":
                task.update(status="blocked", lease_until=None)
            else:
                raise AssertionError(action)
            task["version"] += 1
            self.task = task
            self.actions.append(copy.deepcopy(body))
            self.receipts[key] = (copy.deepcopy(body), copy.deepcopy(task))
            if action == "submit" and self.lose_submit_response:
                self.lose_submit_response = False
                raise cli.CoordinationError("simulated lost HTTP response")
            return copy.deepcopy(task)

    def claim(self):
        return self.action(self.task["task_id"], {"action": "claim", "session_id": self.settings.session_id,
                                                "expected_version": self.task["version"], "idempotency_key": "fixture-claim",
                                                "lease_seconds": 300})

    def expire(self):
        with self.lock:
            self.task["lease_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


class Durable:
    def __init__(self):
        self.calls = []

    def enqueue(self, options, *args):
        self.calls.append((options, args))


def run(ledger, settings):
    task = ledger.get(ledger.task["task_id"])
    return worker.run_attempt(ledger, settings, task["task_id"], task["attempt_id"], settings.session_id,
                              worker.input_fingerprint(task))


def test_snapshot_only_submits_for_review_and_reuses_exact_artifact(workspace):
    repo, artifacts, settings = workspace
    ledger = Ledger(repo, settings)
    ledger.claim()
    (repo / "README.txt").write_text("modified\n", encoding="utf-8")
    result = run(ledger, settings)
    path, = artifacts.glob("*.json")
    original = path.read_bytes()
    snapshot = json.loads(original)
    assert snapshot["git"]["dirty"] and snapshot["git"]["sha"]
    assert result["submission_status"] == ledger.task["status"] == "review"
    assert run(ledger, settings) == result
    assert path.read_bytes() == original
    assert [x["action"] for x in ledger.actions].count("submit") == 1


def test_recovery_after_publish_before_checkpoint(workspace, monkeypatch):
    repo, artifacts, settings = workspace
    ledger = Ledger(repo, settings)
    ledger.claim()

    class Crash(BaseException):
        pass

    monkeypatch.setattr(worker, "_test_crash", lambda *_: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        run(ledger, settings)
    path, = artifacts.glob("*.json")
    original = path.read_bytes()
    ledger.expire()
    monkeypatch.setattr(worker, "_test_crash", lambda *_: None)
    monkeypatch.setattr(worker, "git_snapshot", lambda *_: pytest.fail("Recovery must reuse the published observation"))
    run(ledger, settings)
    assert path.read_bytes() == original
    assert len(list(artifacts.glob("*.json"))) == 1
    assert "recover" in [x["action"] for x in ledger.actions]
    assert ledger.task["attempt_no"] == 1 and ledger.task["status"] == "review"


def test_lost_submit_response_reconciles_committed_evidence(workspace):
    repo, _, settings = workspace
    ledger = Ledger(repo, settings)
    ledger.claim()
    ledger.lose_submit_response = True
    with pytest.raises(cli.CoordinationError):
        run(ledger, settings)
    assert run(ledger, settings)["submission_status"] == "review"
    assert [x["action"] for x in ledger.actions].count("submit") == 1


def test_rejected_submit_is_an_error_not_a_terminal_result(workspace):
    repo, _, settings = workspace
    ledger = Ledger(repo, settings)
    ledger.claim()
    ledger.reject_submit = True
    with pytest.raises(cli.CoordinationError, match="submit_forbidden"):
        run(ledger, settings)
    assert ledger.task["status"] == "running"


@pytest.mark.parametrize("change", ["cancelled", "new_attempt", "new_session", "changed_input"])
def test_fences_cancelled_or_reassigned_attempts(workspace, change):
    repo, artifacts, settings = workspace
    ledger = Ledger(repo, settings)
    task = ledger.claim()
    if change == "cancelled":
        ledger.task["status"] = "cancelled"
    elif change == "new_attempt":
        ledger.task["attempt_id"] = str(uuid4())
    elif change == "new_session":
        ledger.task["worker"]["session_id"] = "other"
    else:
        ledger.task["acceptance"] = "changed"
    with pytest.raises(worker.FencedAttempt):
        worker.run_attempt(ledger, settings, task["task_id"], task["attempt_id"], settings.session_id, worker.input_fingerprint(task))
    assert not list(artifacts.glob("*.json"))


def test_cancellation_between_git_read_and_publication(workspace, monkeypatch):
    repo, artifacts, settings = workspace
    ledger = Ledger(repo, settings)
    ledger.claim()
    original = worker.git_snapshot

    def cancel_after_read(path):
        result = original(path)
        ledger.task["status"] = "cancelled"
        ledger.task["version"] += 1
        return result

    monkeypatch.setattr(worker, "git_snapshot", cancel_after_read)
    with pytest.raises(worker.FencedAttempt):
        run(ledger, settings)
    assert not list(artifacts.glob("*.json"))


def test_artifact_conflict_is_not_overwritten(workspace):
    repo, artifacts, settings = workspace
    ledger = Ledger(repo, settings)
    task = ledger.claim()
    path = worker.artifact_path(settings, task["task_id"], task["attempt_id"])
    path.write_text('{"foreign":"artifact"}', encoding="utf-8")
    with pytest.raises(worker.ArtifactConflict):
        run(ledger, settings)
    assert json.loads(path.read_text()) == {"foreign": "artifact"}
    assert ledger.task["status"] == "running"


def test_worktree_allowlist_and_exact_root(workspace, tmp_path):
    repo, _, settings = workspace
    for path in [str(tmp_path), str(tmp_path / "worktree-sibling"), ".", None]:
        with pytest.raises((ValueError, FileNotFoundError)):
            worker.allowed_worktree({"worktree": path}, settings)
    nested = repo / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="exact Git"):
        worker.git_snapshot(nested)


def test_symlink_cannot_escape_allowlist(workspace, tmp_path):
    repo, _, settings = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant directory symlink creation")
    with pytest.raises(ValueError, match="allowlist"):
        worker.allowed_worktree({"worktree": str(link)}, settings)


def test_repository_filter_commands_are_disabled(workspace):
    repo, _, _ = workspace
    marker = repo / "forbidden-filter-ran.txt"
    git(repo, "config", "filter.trip.clean", f'echo forbidden > "{marker.as_posix()}"')
    git(repo, "config", "filter.trip.required", "true")
    (repo / ".gitattributes").write_text("README.txt filter=trip\n", encoding="utf-8")
    (repo / "README.txt").write_text("modified to force content comparison\n", encoding="utf-8")
    assert worker.git_snapshot(repo)["dirty"]
    assert not marker.exists()


def test_reconcile_claim_without_workflow_and_no_queued_polling(workspace):
    repo, _, settings = workspace
    ledger, durable = Ledger(repo, settings), Durable()
    assert worker.reconcile_running(ledger, durable, settings) == 0
    task = ledger.claim()  # Crash gap: no durable enqueue exists.
    assert worker.reconcile_running(ledger, durable, settings) == 1
    worker.enqueue_task(ledger, durable, settings, task["task_id"])
    assert durable.calls[0] == durable.calls[1]
    assert durable.calls[0][0]["workflow_id"] == worker.workflow_id(task["task_id"], task["attempt_id"])
    assert all(isinstance(value, str) for value in durable.calls[0][1])
    assert ledger.task["attempt_no"] == 1


def test_cached_claim_cannot_authorize_stale_attempt(workspace):
    repo, _, settings = workspace
    ledger, durable = Ledger(repo, settings), Durable()
    original = ledger.action

    def claim_then_replace(task_id, body):
        result = original(task_id, body)
        ledger.task["worker"] = {"agent_id": "different", "session_id": "different"}
        ledger.task["attempt_id"] = str(uuid4())
        return result

    ledger.action = claim_then_replace
    with pytest.raises(worker.FencedAttempt):
        worker.enqueue_task(ledger, durable, settings, ledger.task["task_id"])
    assert durable.calls == []


def test_all_running_pages_are_followed():
    client = cli.CoordinationClient("http://127.0.0.1:1", "isolated-test-key")
    calls = []

    def page(**kwargs):
        calls.append(kwargs)
        return {"tasks": [{"number": len(calls)}], "next_cursor": "next" if len(calls) == 1 else None}

    client.list = page
    assert list(client.iter_tasks(status="running")) == [{"number": 1}, {"number": 2}]
    assert calls[1]["cursor"] == "next" and all(x["status"] == "running" for x in calls)


def test_cli_reads_older_history_pages_in_chronological_order(monkeypatch, capsys):
    client = cli.CoordinationClient("http://127.0.0.1:1", "isolated-test-key")
    task_id = str(uuid4())
    calls = []

    def request(method, path):
        parsed = urlsplit(path)
        params = parse_qs(parsed.query)
        before = int(params["before_version"][0])
        limit = int(params["limit"][0])
        assert method == "GET" and parsed.path == f"/{task_id}/history"
        calls.append((before, limit))
        remaining = [version for version in range(5, 0, -1) if version < before]
        page = remaining[:limit]
        return {"events": [{"version": version, "evidence": f"evidence-{version}"} for version in reversed(page)],
                "next_cursor": page[-1] if len(remaining) > limit else None, "limit": limit}

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(cli.CoordinationClient, "from_env", lambda _url=None: client)
    assert cli.main(["get", task_id, "--all-history", "--before-version", "6", "--limit", "2"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [event["version"] for event in output["events"]] == [1, 2, 3, 4, 5]
    assert output["events"][0]["evidence"] == "evidence-1" and output["next_cursor"] is None
    assert calls == [(6, 2), (4, 2), (2, 2)]


def test_cli_history_page_preserves_cursor_and_empty_result(monkeypatch, capsys):
    client = cli.CoordinationClient("http://127.0.0.1:1", "isolated-test-key")
    task_id = str(uuid4())
    page = {"events": [], "next_cursor": None, "limit": 2}

    def request(method, path):
        assert method == "GET" and path == f"/{task_id}/history?limit=2&before_version=1"
        return page

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(cli.CoordinationClient, "from_env", lambda _url=None: client)
    assert cli.main(["get", task_id, "--history", "--before-version", "1", "--limit", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == page


def test_history_repeated_cursor_is_an_error_not_an_infinite_loop(monkeypatch):
    client = cli.CoordinationClient("http://127.0.0.1:1", "isolated-test-key")
    monkeypatch.setattr(client, "history", lambda *_args, **_kwargs: {"events": [{"version": 4}], "next_cursor": 4})
    with pytest.raises(cli.CoordinationError, match="history cursor"):
        list(client.iter_history(str(uuid4())))


def test_cli_requires_session_and_reports_errors_without_key(monkeypatch, capsys, tmp_path):
    key = "test-only-credential-not-for-workflow"
    monkeypatch.setenv("COGCORE_COORD_API_KEY", key)
    monkeypatch.delenv("COGCORE_COORD_SESSION_ID", raising=False)
    monkeypatch.delenv("COGCORE_SESSION_ID", raising=False)
    payload = tmp_path / "task.json"
    payload.write_text('{"idempotency_key":"test-create-001"}', encoding="utf-8")
    assert cli.main(["create", "--json", str(payload)]) == 2
    captured = capsys.readouterr()
    assert "session_id" in captured.err and key not in captured.err + captured.out


def test_credentials_are_not_forwarded_on_redirect(workspace):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/credential-trap")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = cli.CoordinationClient(f"http://127.0.0.1:{server.server_port}", "isolated-test-key")
        with pytest.raises(cli.CoordinationError) as failure:
            client.list()
        assert failure.value.status == 302 and requests == ["/coord/tasks?limit=200"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_truncated_transport_errors_are_retryable_and_sanitized(monkeypatch):
    client = cli.CoordinationClient("http://127.0.0.1:1", "isolated-test-key")

    def truncated(*_args, **_kwargs):
        raise IncompleteRead(b"untrusted partial response", 100)

    monkeypatch.setattr(client._opener, "open", truncated)
    with pytest.raises(cli.CoordinationError, match="connection failed") as failure:
        client.list()
    assert failure.value.retryable


def wait_for(predicate, *, timeout=60):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for isolated worker state")


@pytest.mark.parametrize("crash_stage", ["after_claim", "after_publish"])
def test_postgres_dbos_process_crash_recovery(workspace, tmp_path, crash_stage):
    """Real kill/restart; fake Core stays alive while the DBOS process dies."""
    database_url = os.environ.get("COGCORE_COORD_TEST_DB_URL")
    if not database_url:
        pytest.skip("Set COGCORE_COORD_TEST_DB_URL to a dedicated test_ PostgreSQL database")
    pytest.importorskip("dbos")
    psycopg = pytest.importorskip("psycopg")
    assert urlsplit(database_url).path.rsplit("/", 1)[-1].startswith("test_"), "Only isolated test_ databases are accepted"
    repo, artifacts, base_settings = workspace
    unique = uuid4().hex[:12]
    settings = replace(base_settings, database_url=database_url, session_id="test-session-" + unique,
                       executor_id="test-executor-" + unique, application_name="coord-test-" + unique,
                       application_version="snapshot-test-" + unique)
    ledger = Ledger(repo, settings)
    api_key = "isolated-key-" + uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            try:
                if self.headers.get("X-API-Key") != api_key:
                    raise cli.CoordinationError("invalid_key", 401)
                parsed = urlsplit(self.path)
                if parsed.path == "/coord/tasks":
                    params = parse_qs(parsed.query)
                    result = {"tasks": list(ledger.iter_tasks(status=params.get("status", [None])[0])), "next_cursor": None}
                elif parsed.path == "/coord/tasks/" + ledger.task["task_id"]:
                    result = ledger.get(ledger.task["task_id"])
                elif parsed.path == "/coord/tasks/" + ledger.task["task_id"] + "/actions":
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    result = ledger.action(ledger.task["task_id"], body)
                else:
                    raise cli.CoordinationError("not_found", 404)
                code = 200
            except cli.CoordinationError as exc:
                code, result = exc.status or 503, {"detail": str(exc)}
            data = json.dumps(result).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    environment = dict(os.environ)
    environment.update(COGCORE_COORD_DB_URL=database_url, COGCORE_COORD_API_URL=f"http://127.0.0.1:{server.server_port}",
                       COGCORE_COORD_API_KEY=api_key, COGCORE_COORD_AGENT_ID=settings.agent_id,
                       COGCORE_COORD_SESSION_ID=settings.session_id, COGCORE_COORD_EXECUTOR_ID=settings.executor_id,
                       COGCORE_COORD_ALLOWED_ROOTS=json.dumps([str(repo)]), COGCORE_COORD_ARTIFACTS_DIR=str(artifacts),
                       COGCORE_COORD_APP_NAME=settings.application_name, COGCORE_COORD_APP_VERSION=settings.application_version,
                       COGCORE_COORD_CONCURRENCY="1", COGCORE_COORD_RECONCILE_SECONDS="1", COGCORE_COORD_TEST_MODE="1",
                       PYTHONIOENCODING="utf-8")
    environment.pop("COGCORE_COORD_TEST_CRASH", None)
    environment.pop("COGCORE_COORD_PROJECT", None)
    processes, handles = [], []
    root = Path(__file__).resolve().parents[1]

    def launch(label, fault=None):
        logfile = tmp_path / (label + ".log")
        handle = logfile.open("wb")
        handles.append(handle)
        env = dict(environment)
        if fault:
            env["COGCORE_COORD_TEST_CRASH"] = fault
        process = subprocess.Popen([sys.executable, "-m", "tools.coordination_worker", "start"], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        # A fresh DBOS database performs many schema migrations; SSH test tunnels
        # can make these individual round trips much slower than localhost.
        wait_for(lambda: '"event": "worker_ready"' in logfile.read_text(encoding="utf-8", errors="replace") or process.poll() is not None,
                 timeout=180)
        assert process.poll() is None or (fault and process.poll() == 86), "Worker failed before readiness; inspect the isolated pytest temp log"
        return process

    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)

    try:
        initial = launch("initialize")
        stop(initial)
        enqueue_env = dict(environment)
        if crash_stage == "after_claim":
            enqueue_env["COGCORE_COORD_TEST_CRASH"] = crash_stage
        queued = subprocess.run([sys.executable, "-m", "tools.coordination_worker", "enqueue", ledger.task["task_id"]],
                                cwd=root, env=enqueue_env, capture_output=True, timeout=30)
        assert queued.returncode == (86 if crash_stage == "after_claim" else 0)
        original = None
        if crash_stage == "after_publish":
            doomed = launch("crash-after-publish", crash_stage)
            assert doomed.wait(timeout=60) == 86
            path, = artifacts.glob("*.json")
            original = path.read_bytes()
        assert ledger.get(ledger.task["task_id"])["status"] == "running"
        ledger.expire()
        recovered = launch("recovered")
        wait_for(lambda: ledger.get(ledger.task["task_id"])["status"] == "review")
        task = ledger.get(ledger.task["task_id"])
        identifier = worker.workflow_id(task["task_id"], task["attempt_id"])

        def execution_succeeded():
            with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://")) as connection:
                row = connection.execute("SELECT status FROM dbos.workflow_status WHERE workflow_uuid=%s", (identifier,)).fetchone()
                return row and row[0] == "SUCCESS"

        wait_for(execution_succeeded)
        stop(recovered)
        path, = artifacts.glob("*.json")
        if original is not None:
            assert path.read_bytes() == original
        actions = [x["action"] for x in ledger.actions]
        assert actions.count("claim") == actions.count("submit") == 1 and "recover" in actions
        assert task["attempt_no"] == 1 and task["status"] == "review"
        with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://")) as connection:
            workflow_row = connection.execute("SELECT to_jsonb(w)::text FROM dbos.workflow_status w WHERE workflow_uuid=%s", (identifier,)).fetchone()[0]
            input_row = connection.execute("SELECT inputs FROM dbos.workflow_input WHERE workflow_uuid=%s", (identifier,)).fetchone()
            output_rows = connection.execute("SELECT to_jsonb(w)::text FROM dbos.workflow_output w WHERE workflow_uuid=%s", (identifier,)).fetchall()
            step_rows = connection.execute("SELECT to_jsonb(o)::text FROM dbos.operation_outputs o WHERE workflow_uuid=%s", (identifier,)).fetchall()
        # 3.5.0 stores payloads in workflow_input/output, with nullable legacy columns
        # still present in workflow_status. Inspect the actual payload tables too.
        inputs = input_row[0] if input_row else json.loads(workflow_row)["inputs"]
        assert inputs and api_key not in inputs and database_url not in inputs and api_key not in workflow_row
        assert all(api_key not in row[0] for row in [*output_rows, *step_rows])
        assert str(repo) not in inputs  # args are IDs/session/fingerprint only
        assert all(api_key not in log.read_text(encoding="utf-8", errors="replace") for log in tmp_path.glob("*.log"))
    finally:
        for process in processes:
            stop(process)
        for handle in handles:
            handle.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
