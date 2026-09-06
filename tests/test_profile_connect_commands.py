"""Команды подключения после создания помощника — актуальные транспорты.

06.09: карточка показывала `claude mcp add --transport sse …/mcp/sse` — легаси
транспорт, снятый спекой MCP 2026-07 (у нас есть Streamable HTTP `POST /mcp`).
Для Codex команды не было вовсе — GPT-агенты владельца жили без памяти.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE = (ROOT / "sandbox" / "profile.html").read_text(encoding="utf-8")


def test_claude_command_uses_streamable_http():
    assert "claude mcp add --transport http cognitive-core https://mcp.me-ai.ru/mcp " in PROFILE
    assert "--transport sse" not in PROFILE and "/mcp/sse" not in PROFILE


def test_codex_command_uses_bearer_env_var():
    assert "codex mcp add cognitive-core --url https://mcp.me-ai.ru/mcp --bearer-token-env-var COGCORE_CODEX_KEY" in PROFILE
    assert 'setx COGCORE_CODEX_KEY' in PROFILE
