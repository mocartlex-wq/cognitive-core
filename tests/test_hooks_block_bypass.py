"""block_no_verify: обходы гейта, которые хук обязан ловить.

Найдено при разборе конфигурации агента AI-CRM. У них условие на короткий флаг
требовало, чтобы «n» была ПОСЛЕДНЕЙ буквой кластера — `-an` ловился, `-nm` нет.
Проверено на репозитории с падающим pre-commit: `git commit -nm x` коммитил.
У нас было хуже: короткого флага не проверялось вовсе, ловился только
`--no-verify`.

Кейсы намеренно написаны от команды, а не от регулярного выражения: тест,
выведенный из той же модели, что и код, наследует её слепое пятно — именно
так 117 проверок их самотеста остались зелёными при живом обходе.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block_no_verify.py"

BLOCKED = [
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -nm x",                     # валидная склейка -n -m
    "git commit -anm x",
    "git commit -na",
    "git -c core.hooksPath=/dev/null commit -m x",   # гейт не запускается вовсе
    # Git принимает однозначные сокращения длинных флагов. Проверено на
    # репозитории с падающим pre-commit: обе формы коммитят мимо гейта.
    "git commit --no-veri -m x",
    "git commit --no-verif -m x",
    "git push --no-verif",
]

ALLOWED = [
    "git commit -am 'правка'",
    'git commit -m "use -n flag"',          # -n в тексте сообщения
    "git commit --amend --no-edit",
    "git commit --dry-run",
    "git commit --no-verbose -m x",         # законный флаг, гейт при нём работает
    "git push -n",                          # у push -n это --dry-run
    "git status",
    "pytest -q",
]


def _decision(command: str) -> str | None:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.skipif(not HOOK.exists(), reason="хук отсутствует")
@pytest.mark.parametrize("command", BLOCKED)
def test_bypass_is_blocked(command: str):
    assert _decision(command) == "deny", f"обход прошёл мимо хука: {command}"


@pytest.mark.skipif(not HOOK.exists(), reason="хук отсутствует")
@pytest.mark.parametrize("command", ALLOWED)
def test_legitimate_command_passes(command: str):
    assert _decision(command) is None, f"ложное срабатывание: {command}"
