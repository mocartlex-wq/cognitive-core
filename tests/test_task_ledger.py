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

def test_reference_mode_checks_final_artifact_and_survives_retry(queued, tmp_path):
    import hashlib
    from zipfile import ZipFile

    reference = tmp_path / "approved.zip"
    final = tmp_path / "deliverable.zip"
    loose = tmp_path / "attachment.txt"
    loose.write_bytes(b"annex")
    with ZipFile(reference, "w") as z:
        z.writestr("title.txt", "cover")
        z.writestr(loose.name, loose.read_bytes())
    with ZipFile(final, "w") as z:
        z.writestr("title.txt", "cover")

    def artifact(path):
        return {"uri": path.as_uri(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    contract = {"reference": artifact(reference), "deliverable_uri": final.as_uri(),
                "criteria": {"title": "Title matches the approved sample", "annex": "Annex included inside ZIP"}}
    queued["reference_acceptance"] = contract

    def inspect():
        findings = {}
        with ZipFile(final) as actual, ZipFile(reference) as sample:
            for key, name in [("title", "title.txt"), ("annex", loose.name)]:
                present = name in actual.namelist()
                match = present and actual.read(name) == sample.read(name)
                findings[key] = {"location": name, "observed": "matches sample" if match else "not identical or absent",
                                 "outcome": "matched" if match else "different" if present else "missing"}
        return {"artifact": artifact(final), "reference_sha256": artifact(reference)["sha256"], "findings": findings}

    def submit(task, inspection):
        return action(task, "submit", attempt_id=task["attempt_id"], detail="Compared", evidence=EVIDENCE,
                      inspection=inspection)

    def review(task, inspection, verdict="approve", lesson=None):
        return action(task, "review", REVIEWER, verdict=verdict, detail="Independent inspection", evidence=EVIDENCE,
                      inspection=inspection, lesson=lesson)

    task = action(queued, "claim")
    first = inspect()
    assert loose.exists() and first["findings"]["annex"]["outcome"] == "missing"
    with pytest.raises(HTTPException, match="unconfirmed_criterion"):
        submit(task, first)
    with pytest.raises(HTTPException, match="every_criterion"):
        submit(task, {**first, "findings": {"title": first["findings"]["title"]}})
    with pytest.raises(HTTPException, match="inspect_the_declared"):
        submit(task, {**first, "artifact": artifact(loose)})
    # A plausible but false producer declaration is exposed by independent inspection.
    claimed = {**first, "findings": {**first["findings"], "annex": {**first["findings"]["annex"], "outcome": "matched"}}}
    waiting = submit(task, claimed)
    blocked = review(waiting, inspect(), "reject")
    assert blocked["status"] == "blocked" and "lesson" not in blocked["review"]
    assert review(waiting, None, "reject")["status"] == "blocked"
    queued_again = action(blocked, "retry", COORD, reconciliation="Rebuild final file")
    assert "inspection" not in queued_again and queued_again["reference_acceptance"] == contract
    with ZipFile(final, "a") as z:
        z.writestr(loose.name, loose.read_bytes())
    waiting = submit(action(queued_again, "claim"), inspect())
    with pytest.raises(HTTPException, match="reference_digest"):
        review(waiting, {**inspect(), "reference_sha256": "0" * 64})
    with pytest.raises(HTTPException, match="differs_from_submission"):
        review(waiting, claimed)
    with pytest.raises(HTTPException, match="lesson_required"):
        review(waiting, inspect())
    lesson = "Open the final archive by digest and compare the embedded entries with the pinned sample."
    done = review(waiting, inspect(), lesson=lesson)
    assert done["status"] == "verified_done" and done["review"]["lesson"] == lesson


def test_legacy_coordination_receipt_hashes_are_unchanged():
    from app.services.task_ledger import fingerprint, request_hash

    legacy = TaskCreate(idempotency_key="old-create", session_id="coordinator", project="x",
                        title="x", acceptance="x", next_step="x")
    claim = TaskAction(idempotency_key="old-claim", session_id="coordinator", expected_version=0, action="claim")
    for body, added in [(legacy, {"reference_acceptance"}), (claim, {"inspection", "lesson"})]:
        before_upgrade = body.model_dump(mode="json", exclude=added)
        assert request_hash(body, COORD) == fingerprint({"actor": COORD, "body": before_upgrade})
