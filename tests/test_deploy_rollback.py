"""auto-deploy.sh: откат должен срабатывать и при провале conditional_reload
(сборка / миграция / nginx -t), а не только после smoke-теста.

До 2026-09-05 строка `"$REPO_DIR/scripts/conditional_reload.sh" "$PREV" "$NEW"`
стояла без защиты под `set -e`: битая сборка убивала скрипт до smoke, checkout
уже стоял на NEW, следующий тик видел PREV=NEW и молчал. Прод оставался на
старом контейнере с новым кодом в checkout — деплой «молча мёртв».

Тест собирает настоящий git-репозиторий с bare-origin, подменяет
conditional_reload.sh управляемой заглушкой и `sleep` пустышкой, а /health —
файлом. Docker не нужен. Прогоняется bash'ем (Linux CI, Git Bash на Windows).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTO_DEPLOY = REPO_ROOT / "scripts" / "auto-deploy.sh"
COND_RELOAD = REPO_ROOT / "scripts" / "conditional_reload.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash не найден")

FAKE_RELOAD = r"""#!/usr/bin/env bash
# Заглушка conditional_reload.sh: пишет вызовы в журнал, режим берёт из файла.
CTRL="${DEPLOY_TEST_CTRL:?}"
echo "call $1 $2 migrate=${COGNITIVE_MIGRATE:-unset}" >> "$CTRL/reload.log"
mode=$(cat "$CTRL/reload.mode" 2>/dev/null || echo ok)
if [ "$mode" = "fail-forward" ] && [ "$1" = "$(cat "$CTRL/prev")" ]; then
    echo "fake: build FAILED" >&2
    exit 1
fi
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _posix(p: Path) -> str:
    """Git Bash понимает C:/x/y, но не C:\\x\\y внутри двойных кавычек."""
    return str(p).replace("\\", "/")


@pytest.fixture
def deploy_env(tmp_path: Path):
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fakebin / "sleep").chmod(0o755)

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))

    work = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    (work / "scripts").mkdir()
    (work / "scripts" / "conditional_reload.sh").write_text(FAKE_RELOAD)
    (work / "scripts" / "conditional_reload.sh").chmod(0o755)
    (work / "app").mkdir()
    (work / "app" / "main.py").write_text("v1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "v1")
    _git(work, "push", "-q", "origin", "main")
    prev = _git(work, "rev-parse", "HEAD")

    # Второй коммит только в origin — сервер его ещё не видел.
    pusher = tmp_path / "pusher"
    _git(tmp_path, "clone", "-q", str(origin), str(pusher))
    (pusher / "app" / "main.py").write_text("v2\n")
    _git(pusher, "commit", "-q", "-am", "v2")
    _git(pusher, "push", "-q", "origin", "main")
    new = _git(pusher, "rev-parse", "HEAD")

    (ctrl / "prev").write_text(prev)
    health = ctrl / "health.json"
    health.write_text('{"healthy":true}')

    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
        "COGNITIVE_REPO_DIR": _posix(work),
        "COGNITIVE_HEALTH_CMD": f"cat {_posix(health)}",
        "COGNITIVE_SMOKE_ATTEMPTS": "3",
        "COGNITIVE_SMOKE_MIN_OK": "2",
        "COGNITIVE_SMOKE_WARMUP": "1",
        "DEPLOY_TEST_CTRL": _posix(ctrl),
    }
    env.pop("TELEGRAM_BOT_TOKEN", None)
    return {"work": work, "ctrl": ctrl, "prev": prev, "new": new, "env": env,
            "health": health}


def _run(env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, _posix(AUTO_DEPLOY)], env=env["env"], capture_output=True,
        text=True, timeout=120,
    )


def test_scripts_parse():
    for script in (AUTO_DEPLOY, COND_RELOAD):
        r = subprocess.run([BASH, "-n", _posix(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_happy_path_lands_on_new(deploy_env):
    r = _run(deploy_env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(deploy_env["work"], "rev-parse", "HEAD") == deploy_env["new"]
    log = (deploy_env["ctrl"] / "reload.log").read_text()
    assert log.strip() == f"call {deploy_env['prev']} {deploy_env['new']} migrate=unset"


def test_reload_failure_rolls_back_before_smoke(deploy_env):
    (deploy_env["ctrl"] / "reload.mode").write_text("fail-forward")
    r = _run(deploy_env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "conditional_reload FAILED" in r.stdout
    assert "ROLLED BACK successfully" in r.stdout
    assert _git(deploy_env["work"], "rev-parse", "HEAD") == deploy_env["prev"]
    calls = (deploy_env["ctrl"] / "reload.log").read_text().splitlines()
    assert calls == [
        f"call {deploy_env['prev']} {deploy_env['new']} migrate=unset",
        # откат: обратное направление и БЕЗ миграции схемы
        f"call {deploy_env['new']} {deploy_env['prev']} migrate=0",
    ]
    # smoke до отката не запускался
    assert "smoke #" not in r.stdout


def test_smoke_failure_still_rolls_back(deploy_env):
    deploy_env["health"].write_text('{"healthy":false}')
    r = _run(deploy_env)
    # rollback тоже видит healthy:false → «degraded», код 2; главное — HEAD откатился
    assert r.returncode in (1, 2), r.stdout + r.stderr
    assert "SMOKE FAILED" in r.stdout
    assert _git(deploy_env["work"], "rev-parse", "HEAD") == deploy_env["prev"]
