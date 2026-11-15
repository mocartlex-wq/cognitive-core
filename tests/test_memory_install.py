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
