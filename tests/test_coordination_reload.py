"""Execute the actual reload script with isolated command stubs, no host changes."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="Linux host deployment script")


def run_reload(tmp_path, *, invalid=False, restart_fails=False, already_installed=False):
    binary = tmp_path / "bin"
    binary.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (scripts / "cognitive-orchestrator.py").write_text("def invalid(" if invalid else "print('test')", encoding="utf-8")
    stubs = {
        "git": "#!/bin/sh\nprintf '%s\\n' app/main.py scripts/cognitive-orchestrator.py\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
        "cmp": "#!/bin/sh\nexit \"$TEST_COMPARE_EXIT\"\n",
        "sudo": "#!/bin/sh\nprintf '%s\\n' \"sudo $*\" >> \"$TEST_COMMANDS\"\n"
                "if [ \"$*\" = 'systemctl restart cognitive-orchestrator' ] && [ \"$TEST_RESTART_FAIL\" = 1 ]; then exit 43; fi\n",
        "docker": "#!/bin/sh\nprintf '%s\\n' \"docker $*\" >> \"$TEST_COMMANDS\"\n"
                  "case \"$*\" in *'config --services'*) printf 'api\\n';; esac\n",
    }
    for name, text in stubs.items():
        target = binary / name
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)
    log = tmp_path / "commands.txt"
    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "COGNITIVE_REPO_DIR": str(tmp_path),
           "TEST_COMMANDS": str(log), "TEST_RESTART_FAIL": "1" if restart_fails else "0",
           "TEST_COMPARE_EXIT": "0" if already_installed else "1"}
    result = subprocess.run(["bash", str(ROOT / "scripts/conditional_reload.sh"), "old", "new"], env=env, capture_output=True, text=True, timeout=20)
    return result, log.read_text() if log.exists() else ""


def test_host_daemon_updates_before_api_reload(tmp_path):
    result, commands = run_reload(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "sudo install -m 0755" in commands
    assert commands.index("systemctl restart cognitive-orchestrator") < commands.index("docker compose")
    assert " up -d --build api" in commands
    assert "systemctl is-active --quiet cognitive-orchestrator" in commands


def test_invalid_daemon_never_installed_or_api_rebuilt(tmp_path):
    result, commands = run_reload(tmp_path, invalid=True)
    assert result.returncode != 0
    assert "sudo install" not in commands
    assert "docker compose" not in commands


def test_failed_restart_stops_api_rollout(tmp_path):
    result, commands = run_reload(tmp_path, restart_fails=True)
    assert result.returncode == 43
    assert "docker compose" not in commands


def test_equal_installed_file_still_restarts_and_checks_process(tmp_path):
    result, commands = run_reload(tmp_path, already_installed=True)
    assert result.returncode == 0, result.stderr
    assert "sudo install" not in commands
    assert commands.index("systemctl restart cognitive-orchestrator") < commands.index("docker compose")
    assert "systemctl is-active --quiet cognitive-orchestrator" in commands


def test_failed_restart_after_previous_install_still_blocks_rollout(tmp_path):
    result, commands = run_reload(tmp_path, already_installed=True, restart_fails=True)
    assert result.returncode == 43
    assert "sudo install" not in commands
    assert "docker compose" not in commands
