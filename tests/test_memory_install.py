from tools.install_memory_hooks import merge_hooks


def test_install_preserves_permissions_and_unrelated_mixed_hooks():
    original = {"permissions": {"deny": ["secret"]}, "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python cogcore_checkpoint.py"},
        {"type": "command", "command": "existing-protection.exe"}]}]}}
    updated = merge_hooks(original, "python memory_hook.py --agent codex-desktop")
    assert updated["permissions"] == original["permissions"]
    commands = [h["command"] for g in updated["hooks"]["Stop"] for h in g["hooks"]]
    assert commands == ["existing-protection.exe", "python memory_hook.py --agent codex-desktop"]
    assert merge_hooks(updated, "python memory_hook.py --agent codex-desktop") == updated
    assert len(original["hooks"]["Stop"][0]["hooks"]) == 2, "Do not mutate the caller's config"


def test_windows_command_roundtrip_in_cmd_and_git_bash(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from tools.install_memory_hooks import hook_command

    bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name != "nt" or not bash.exists():
        pytest.skip("Windows and Git Bash integration check")
    script = tmp_path / "прочесть.py"
    script.write_text("import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))", encoding="utf-8")
    config = tmp_path / "папка с пробелом" / "настройки.json"
    command = hook_command(sys.executable, script, config, "claude-code")
    expected = ["--config", config.as_posix(), "--agent", "claude-code"]
    # cmd /c consumes its remaining raw command line, not CRT-escaped argv.
    cmd_line = subprocess.list2cmdline([os.environ["COMSPEC"]]) + ' /d /s /c "' + command + '"'
    for shell in ([str(bash), "-c", command], cmd_line):
        result = subprocess.run(shell, capture_output=True, encoding="utf-8", timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == expected
