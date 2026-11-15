"""Behavioural tests: fencing, independent review, cancellation and retry."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.models.coordination import Evidence, TaskAction, TaskCreate
from app.services.task_ledger import transition

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
COORD = {"agent_id": "codex", "session_id": "coordinator"}
WORKER = {"agent_id": "codex", "session_id": "worker-one"}
REVIEWER = {"agent_id": "codex", "session_id": "reviewer-two"}
EVIDENCE = [{"uri": "qa://task/report.json", "description": "Сверен результат", "sha256": "a" * 64}]


@pytest.fixture
def queued():
    return {"task_id": "11111111-1111-4111-8111-111111111111", "status": "queued", "version": 0,
            "coordinator": COORD, "worker": None, "attempt_id": None, "attempt_no": 0,
            "lease_until": None, "acceptance": "Report matches the repository", "next_step": "Run", "evidence": []}


def action(task, verb, actor=WORKER, **kw):
    body = TaskAction(idempotency_key="operation-key-123", session_id=actor["session_id"],
                      expected_version=kw.pop("expected_version", task["version"]), action=verb, **kw)
    return transition(task, body, actor, now=NOW)


def test_complete_requires_another_session_and_review_evidence(queued):
    running = action(queued, "claim")
    review = action(running, "submit", attempt_id=running["attempt_id"], detail="Артефакт готов", evidence=EVIDENCE)
    assert review["status"] == "review"
    with pytest.raises(HTTPException, match="independent_session"):
        action(review, "review", verdict="approve", detail="Проверил сам", evidence=EVIDENCE)
    with pytest.raises(HTTPException, match="review_evidence"):
        action(review, "review", REVIEWER, verdict="approve", detail="Проверил")
    done = action(review, "review", REVIEWER, verdict="approve", detail="Сравнил независимо", evidence=EVIDENCE)
    assert done["status"] == "verified_done"
    assert done["review"]["actor"] == REVIEWER
    assert done["worker"] == WORKER


def test_expired_attempt_must_reconcile_before_recovery(queued):
    running = action(queued, "claim")
    running["lease_until"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(HTTPException, match="stale_attempt"):
        action(running, "heartbeat", attempt_id=running["attempt_id"])
    with pytest.raises(HTTPException, match="reconciliation"):
        action(running, "recover", attempt_id=running["attempt_id"])
    recovered = action(running, "recover", attempt_id=running["attempt_id"], reconciliation="Артефакт ещё не создан")
    assert recovered["attempt_id"] == running["attempt_id"]
    assert datetime.fromisoformat(recovered["lease_until"]) > NOW


@pytest.mark.parametrize("verb", ["heartbeat", "recover", "submit", "block", "fail"])
def test_other_session_cannot_control_worker_attempt(queued, verb):
    running = action(queued, "claim")
    with pytest.raises(HTTPException):
        action(running, verb, REVIEWER, attempt_id=running["attempt_id"],
               detail="x", evidence=EVIDENCE, reconciliation="checked")


def test_cancel_fences_late_result_and_cannot_be_retried_silently(queued):
    running = action(queued, "claim")
    cancelled = action(running, "cancel", COORD, detail="Задача отменена")
    assert cancelled["status"] == "cancelled"
    with pytest.raises(HTTPException):
        action(cancelled, "submit", attempt_id=running["attempt_id"], detail="Поздний ответ", evidence=EVIDENCE)
    with pytest.raises(HTTPException):
        action(cancelled, "retry", COORD, reconciliation="checked")


def test_rejected_work_has_explicit_retry_and_new_attempt(queued):
    first = action(queued, "claim")
    review = action(first, "submit", attempt_id=first["attempt_id"], detail="result", evidence=EVIDENCE)
    blocked = action(review, "review", REVIEWER, verdict="reject", detail="Неверный SHA", evidence=EVIDENCE)
    assert blocked["status"] == "blocked"
    with pytest.raises(HTTPException, match="reconciliation"):
        action(blocked, "retry", COORD)
    retry = action(blocked, "retry", COORD, reconciliation="Проверили прежний файл, он не принят")
    second = action(retry, "claim")
    assert second["attempt_id"] != first["attempt_id"]
    assert second["attempt_no"] == 2
    with pytest.raises(HTTPException):
        action(second, "submit", attempt_id=first["attempt_id"], detail="stale", evidence=EVIDENCE)


def test_queued_assignment_and_dependency_are_enforced(queued):
    assigned = action(queued, "assign", COORD, assigned_session_id="different")
    with pytest.raises(HTTPException, match="assigned_to_another"):
        action(assigned, "claim")
    body = TaskAction(idempotency_key="claim-test", session_id=WORKER["session_id"], expected_version=0, action="claim")
    with pytest.raises(HTTPException, match="not_claimable"):
        transition(queued, body, WORKER, now=NOW, dependencies_ready=False)


def test_cas_and_unknown_fields_reject_silent_changes(queued):
    with pytest.raises(HTTPException, match="stale_version"):
        action(queued, "claim", expected_version=8)
    with pytest.raises(ValueError):
        TaskCreate(idempotency_key="create-key", session_id="one", project="x", title="x", acceptance="x", next_step="x", status="verified_done")


def test_invalid_attempt_is_rejected(queued):
    running = action(queued, "claim")
    with pytest.raises(HTTPException, match="stale_attempt"):
        action(running, "submit", attempt_id=UUID("00000000-0000-0000-0000-000000000001"), detail="x", evidence=EVIDENCE)


def test_empty_claim_does_not_mutate_input(queued):
    running = action(queued, "claim")
    assert queued["worker"] is None
    assert queued["status"] == "queued"
    assert running["worker"] == WORKER


@pytest.mark.parametrize("field", ["uri", "description"])
def test_whitespace_cannot_count_as_evidence(field):
    data = {"uri": "qa://receipt", "description": "Verified result", field: " \t \n "}
    with pytest.raises(ValueError):
        Evidence(**data)


@pytest.mark.parametrize("field", ["session_id", "project", "title", "acceptance", "next_step"])
def test_required_task_contract_is_not_whitespace(field):
    data = dict(idempotency_key="create-key", session_id="one", project="x", title="x", acceptance="x", next_step="x")
    data[field] = " \t "
    with pytest.raises(ValueError):
        TaskCreate(**data)
