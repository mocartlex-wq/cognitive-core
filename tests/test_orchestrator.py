"""Unit tests для app.services.orchestrator — pure functions без сети."""
from __future__ import annotations

import json
import pytest

from app.services.orchestrator import (
    ACTIONS,
    DESTRUCTIVE_ACTIONS,
    OrchestratorConfig,
    build_system_prompt,
    decision_payload,
    expand_mass_dm_threshold,
    is_destructive,
    is_owner_message,
    parse_llm_json,
    sanitize_for_remember,
    should_ignore_message,
    validate_action,
)


# ─── parse_llm_json ───────────────────────────────────────────────────────


def test_parse_pure_json():
    raw = '{"action":"query_status","args":{},"reasoning":"x","confidence":0.9}'
    assert parse_llm_json(raw)["action"] == "query_status"


def test_parse_markdown_fenced():
    raw = '```json\n{"action":"query_status","args":{}}\n```'
    assert parse_llm_json(raw)["action"] == "query_status"


def test_parse_with_preamble():
    raw = 'Конечно! Вот ответ:\n\n{"action":"refuse","args":{"reason":"x"}}'
    assert parse_llm_json(raw)["action"] == "refuse"


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_llm_json("not json at all")


# ─── validate_action ──────────────────────────────────────────────────────


def test_validate_unknown_action_becomes_refuse():
    parsed = {"action": "evil_action", "args": {}, "reasoning": "", "confidence": 0.5}
    r = validate_action(parsed)
    assert r["action"] == "refuse"
    assert not r["valid"]
    assert "evil_action" in r["error"]


def test_validate_missing_required_args_becomes_clarification():
    # send_dm требует to и text
    parsed = {"action": "send_dm", "args": {"to": "rastr"}, "confidence": 0.9}
    r = validate_action(parsed)
    assert r["action"] == "request_clarification"
    assert "text" in r["error"]


def test_validate_optional_args_ok():
    # remember_fact has "result" as optional (description contains "optional")
    parsed = {"action": "remember_fact", "args": {"domain": "x", "task": "y"}, "confidence": 1.0}
    r = validate_action(parsed)
    assert r["valid"], f"should be valid, got {r}"
    assert r["action"] == "remember_fact"


def test_validate_query_status_no_args():
    parsed = {"action": "query_status", "args": {}, "confidence": 1.0}
    r = validate_action(parsed)
    assert r["valid"]


def test_validate_destructive_action_passes_classification():
    """Destructive validation проходит — runtime само добавляет approval gate."""
    parsed = {"action": "delete_agent", "args": {"agent_id": "victim"}, "confidence": 1.0}
    r = validate_action(parsed)
    assert r["valid"]
    assert r["action"] == "delete_agent"


# ─── is_destructive ───────────────────────────────────────────────────────


def test_is_destructive_correct():
    assert is_destructive("delete_agent")
    assert is_destructive("revoke_key")
    assert is_destructive("mass_dm")
    assert is_destructive("purge_data")
    assert not is_destructive("query_status")
    assert not is_destructive("send_dm")
    assert not is_destructive("refuse")


def test_destructive_set_matches_actions_dict():
    """DESTRUCTIVE_ACTIONS должен быть синхронен с ACTIONS dict."""
    derived = {n for n, s in ACTIONS.items() if s["destructive"]}
    assert DESTRUCTIVE_ACTIONS == frozenset(derived)


# ─── broadcast → mass_dm escalation ───────────────────────────────────────


def test_expand_broadcast_small_stays():
    assert expand_mass_dm_threshold("broadcast", {"to": ["a", "b", "c"]}) == "broadcast"


def test_expand_broadcast_large_becomes_mass_dm():
    assert expand_mass_dm_threshold("broadcast", {"to": ["a"] * 10}) == "mass_dm"


def test_expand_other_action_unchanged():
    assert expand_mass_dm_threshold("send_dm", {"to": "rastr"}) == "send_dm"


# ─── should_ignore_message ────────────────────────────────────────────────


def test_ignore_self():
    assert should_ignore_message({"from": "orchestrator", "text": "echo"}, orchestrator_id="orchestrator")


def test_ignore_server_runtime():
    assert should_ignore_message({"from": "server-runtime", "text": "agent joined"}, orchestrator_id="orchestrator")


def test_ignore_system_marker():
    msg = {"from": "rastr", "text": "[from rastr server-runtime ack]"}
    assert should_ignore_message(msg, orchestrator_id="orchestrator")


def test_dont_ignore_real_user_dm():
    assert not should_ignore_message(
        {"from": "cognitive-core-laptop", "text": "статус всех"},
        orchestrator_id="orchestrator",
    )


# ─── sanitize_for_remember ────────────────────────────────────────────────


def test_sanitize_replaces_double_dash():
    assert "--" not in sanitize_for_remember("foo -- bar")


def test_sanitize_replaces_semicolon():
    assert ";" not in sanitize_for_remember("foo; bar")


def test_sanitize_softens_sql_keywords():
    out = sanitize_for_remember("DROP TABLE users; SELECT * FROM x")
    assert "DROP" not in out
    assert "SELECT" not in out


def test_sanitize_handles_empty():
    assert sanitize_for_remember("") == ""
    assert sanitize_for_remember(None) == ""


# ─── decision_payload ─────────────────────────────────────────────────────


def test_decision_payload_shape():
    parsed = {"action": "query_status", "args": {}, "reasoning": "r", "confidence": 0.9}
    p = decision_payload(
        source_agent="owner",
        source_text="статус",
        parsed=parsed,
        requires_approval=False,
        approval_status="not_required",
        execution_result={"ok": True, "message": "12 agents"},
    )
    assert p["source_agent"] == "owner"
    assert p["action"] == "query_status"
    assert p["requires_approval"] is False
    assert p["execution_result"]["ok"]


def test_decision_payload_truncates_long_source_text():
    long_text = "x" * 5000
    p = decision_payload("u", long_text, {"action": "refuse", "args": {}}, requires_approval=False)
    assert len(p["source_text"]) <= 1500


# ─── system prompt ────────────────────────────────────────────────────────


def test_system_prompt_lists_all_actions():
    prompt = build_system_prompt("orchestrator")
    for name in ACTIONS:
        assert name in prompt, f"action {name} missing from prompt"


def test_system_prompt_marks_destructive():
    prompt = build_system_prompt("orchestrator")
    # destructive actions должны иметь маркер
    for name in DESTRUCTIVE_ACTIONS:
        # Каждое destructive должно упоминаться рядом с DESTRUCTIVE словом
        lines = [l for l in prompt.split("\n") if name in l]
        assert any("DESTRUCTIVE" in l for l in lines), f"{name} not marked DESTRUCTIVE in prompt"


# ─── is_owner_message ─────────────────────────────────────────────────────


def test_is_owner_when_match():
    cfg = OrchestratorConfig(owner_agent_id="cognitive-core-laptop")
    assert is_owner_message({"from": "cognitive-core-laptop", "text": "x"}, cfg)


def test_is_owner_when_no_match():
    cfg = OrchestratorConfig(owner_agent_id="cognitive-core-laptop")
    assert not is_owner_message({"from": "stranger", "text": "x"}, cfg)


def test_is_owner_when_unconfigured():
    cfg = OrchestratorConfig(owner_agent_id="")
    assert not is_owner_message({"from": "anyone", "text": "x"}, cfg)
