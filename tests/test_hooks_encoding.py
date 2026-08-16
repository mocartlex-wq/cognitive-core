"""Хуки не должны падать из-за кодировки консоли.

Приём взят у агента AI-CRM: проверять не текст хука, а его выживание на узкой
кодировке. У них все четыре хука падали на Windows с UnicodeEncodeError, и
падали МОЛЧА: харнесс видит ненулевой код возврата и просто не применяет
решение. Защита числится установленной и не работает — худший вид поломки.

Их вариант проверки — прогнать вывод через `str.encode(cp1251)` — годится для
их решения (вывод сделан ASCII-безопасным). Наше решение другое: хук сам
переводит потоки в UTF-8 и продолжает печатать по-русски. Поэтому проверяем
то, что защищает именно нас: запускаем хук с узкой кодировкой, навязанной
через окружение, и требуем нулевой код возврата и разбираемый JSON.

Тест умеет падать: удалите блок reconfigure в любом хуке — упадёт (проверено).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"

# Кодировки, в которых реально работает консоль Windows у владельца и коллег.
# cp1252 — английская локаль: кириллицы нет вовсе, самый жёсткий случай.
NARROW_ENCODINGS = ["cp1251", "cp866", "cp1252", "ascii"]

# Вход, на котором хук ОБЯЗАН что-то напечатать: молчащий хук проверку
# кодировки проходит всегда и потому ничего не доказывает.
PAYLOADS = {
    "protect_secrets.py": {
        "tool_name": "Write",
        "tool_input": {"file_path": "/srv/app/.env"},
    },
    "block_no_verify.py": {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit --no-verify -m правка"},
    },
    "filter_test_output.py": {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
    },
    "ruff_on_save.py": {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(HOOKS / "protect_secrets.py")},
    },
}


def _run(hook: Path, payload: dict, encoding: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING=encoding)
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )


@pytest.mark.parametrize("hook_name", sorted(PAYLOADS))
@pytest.mark.parametrize("encoding", NARROW_ENCODINGS)
def test_hook_survives_narrow_console_encoding(hook_name: str, encoding: str):
    hook = HOOKS / hook_name
    if not hook.exists():
        pytest.skip(f"{hook_name} отсутствует")

    res = _run(hook, PAYLOADS[hook_name], encoding)

    assert res.returncode == 0, (
        f"{hook_name} упал при PYTHONIOENCODING={encoding}\n"
        f"{res.stderr}\n"
        "На такой консоли хук молча не применит решение, оставаясь в списке "
        "установленных."
    )
    assert "UnicodeEncodeError" not in res.stderr, res.stderr


@pytest.mark.parametrize("hook_name", sorted(PAYLOADS))
def test_hook_output_is_valid_json_or_empty(hook_name: str):
    """Отдельно от кодировки: вывод либо пуст, либо разбирается харнессом.

    Обрезанная на середине строка — как раз то, что оставляет после себя
    падение по кодировке, поэтому проверяем разбор, а не только код возврата.
    """
    hook = HOOKS / hook_name
    if not hook.exists():
        pytest.skip(f"{hook_name} отсутствует")

    out = _run(hook, PAYLOADS[hook_name], "utf-8").stdout.strip()
    if out:
        json.loads(out)
