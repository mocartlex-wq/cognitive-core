"""protect_secrets: секреты нельзя перезаписать и нельзя стереть.

23.08. Хук существовал с апреля и разбирал единственное поле — `file_path`.
В settings.json он стоял на `Edit|Write|NotebookEdit`, то есть на Bash не
вызывался вовсе: `rm .env`, `cat > .env`, `git checkout -- .env` проходили
мимо защиты целиком. Ровно этим путём .env на каноне уничтожался трижды, и
каждый раз восстанавливался руками.

Класс дефекта — «правило записано в одном представлении входа»: тот же, за
который я разбирал конфигурацию соседнего агента, и который у себя не увидел,
потому что мерил защиту от УТЕЧКИ, а измерять надо было ещё и сохранность.

Кейсы написаны от команд, а не от регулярного выражения: тест, выведенный из
той же модели, что и код, наследует её слепое пятно.

Отдельно важен список ALLOWED. Защитный хук, который мешает читать, снимают
целиком — и тогда он не защищает ни от чего.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "protect_secrets.py"

BLOCKED_COMMANDS = [
    # уничтожение
    "rm .env",
    "rm -f .env",
    "rm -- .env",
    "sudo rm /etc/nginx/ssl/site.key",
    "rm -rf ~/.ssh/id_ed25519",
    "shred -u .env.production",
    "truncate -s 0 .env",
    # перезапись через перенаправление
    "echo FOO=1 > .env",
    "echo FOO=1 >> .env",
    "echo FOO=1 >.env",                       # без пробела
    "cat template > .env",
    "printf '' > deploy/certs/server.pem",
    "echo 'user:hash' > .htpasswd",
    # копирование поверх
    "cp .env.example .env",
    "mv /tmp/new .env",
    "install -m 600 /tmp/new .env",
    "echo x | tee .env",
    "cat x | tee -a .env",
    # правка на месте
    "sed -i 's/KEY=.*/KEY=new/' .env",
    "sed -i.bak 's/a/b/' .env",
    "dd if=/dev/null of=.env",
    # git возвращает файл к версии из репозитория — то же уничтожение
    "git checkout -- .env",
    "git checkout HEAD -- .env",
    "git restore .env",
    "git clean -fdx",
    "git clean -x",
    # второй сегмент цепочки проверяется наравне с первым
    "cp .env /tmp/copy && rm .env",
    "cd /opt/app; rm .env",
    # инлайн-скрипт
    """python -c "open('.env','w').write('')" """,
    """python3 -c "import os; os.remove('.env')" """,
    """python -c "import shutil; shutil.copy('/tmp/x', '.env')" """,
]

ALLOWED_COMMANDS = [
    # чтение разрешено сознательно: хук, мешающий читать, снимут целиком
    "cat .env",
    "grep API_KEY .env",
    "head -5 .env",
    "sed -n '1,5p' .env",                     # без -i это чтение
    """python -c "print(open('.env').read())" """,
    # .env как ТЕКСТ, а не как цель
    "echo '.env' >> .gitignore",
    'echo "никогда не пиши в > .env руками"',
    "grep -rn 'env' app/",
    "git log --oneline -- .env",
    "git diff .env",
    # шаблон без секретов править можно
    "rm .env.example",
    "echo 'FOO=' > .env.example",
    "cp .env.example /tmp/template",
    # git clean без -x игнорируемые файлы не трогает, а .env игнорируемый
    "git clean -fd",
    "git clean -n",
    # обычная работа
    "git checkout -- app/main.py",
    "cp app/main.py app/main.py.bak",
    "pytest -q",
    "docker compose config",
]

BLOCKED_PATHS = [".env", "app/.env.production", "certs/server.key",
                 "/home/salex/.ssh/id_rsa", "conf/.htpasswd",
                 r"D:\ИИ\cognitive-core\.env"]
ALLOWED_PATHS = [".env.example", "app/main.py", "README.md", "notes.keys.md"]


def _decision(payload: dict) -> str | None:
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def _bash(command: str) -> str | None:
    return _decision({"tool_name": "Bash", "tool_input": {"command": command}})


pytestmark = pytest.mark.skipif(not HOOK.exists(), reason="хук отсутствует")


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_destructive_command_is_blocked(command: str):
    assert _bash(command) == "deny", f"секрет уничтожается мимо хука: {command}"


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_legitimate_command_passes(command: str):
    assert _bash(command) is None, f"ложное срабатывание: {command}"


@pytest.mark.parametrize("path", BLOCKED_PATHS)
def test_write_tool_is_blocked(path: str):
    assert _decision({"tool_name": "Write",
                      "tool_input": {"file_path": path}}) == "deny"


@pytest.mark.parametrize("path", ALLOWED_PATHS)
def test_write_tool_passes(path: str):
    assert _decision({"tool_name": "Write",
                      "tool_input": {"file_path": path}}) is None


def test_notebook_path_key_is_read():
    """NotebookEdit кладёт путь в notebook_path.

    Пока хук читал только file_path, строка «NotebookEdit» в матчере
    settings.json обещала защиту, которой не было ни при каком входе.
    """
    assert _decision({"tool_name": "NotebookEdit",
                      "tool_input": {"notebook_path": "secrets/.env"}}) == "deny"


def test_unparsable_command_fails_closed():
    """Незакрытая кавычка: не падаем и не гадаем, а запрещаем.

    Падение хука = защита снята молча. Грубый разбор тоже не годится: замер
    подменой показал, что он пропускает 5 настоящих уничтожений из 31 и при
    этом блокирует законную команду. Поэтому неразборчивый ввод с упоминанием
    секрета отклоняется.
    """
    assert _bash("""echo "unterminated > .env""") == "deny"


def test_unparsable_command_without_secret_passes():
    """Отказ по неразборчивости не должен превращаться в запрет всего подряд."""
    assert _bash("""echo "unterminated app/main.py""") is None


def test_hook_is_registered_for_bash():
    """Хук без строки в settings.json не вызывается никогда.

    Проверка есть, потому что настоящий дефект был именно здесь: код умел
    разбирать file_path, а Bash в матчер не входил.
    """
    cfg = json.loads((HOOK.parent.parent / "settings.json").read_text(encoding="utf-8"))
    for block in cfg["hooks"]["PreToolUse"]:
        hooks = [h["args"][0].rsplit("/", 1)[-1] for h in block["hooks"]]
        if "protect_secrets.py" in hooks and "Bash" in block["matcher"]:
            return
    pytest.fail("protect_secrets.py не подключён к Bash — код есть, вызова нет")
