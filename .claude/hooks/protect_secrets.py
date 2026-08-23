#!/usr/bin/env python3
"""Запрещает запись в файлы с секретами и их уничтожение.

CLAUDE.md: «Никогда не коммитить .env — в нём реальные ключи API и пароли».
Правило прозой не мешает записать в файл; это — мешает.

Читать не запрещаем, только писать и удалять: агенту иногда нужно свериться
со схемой. Отсюда сознательная граница — `cat .env > /tmp/leak` хук пропустит.
Вынос секрета наружу это отдельный класс, и ловить его тем же условием нельзя:
пришлось бы блокировать `grep KEY .env`, после чего хук снимут целиком.

23.08: до этой правки хук разбирал ТОЛЬКО `file_path`, а в settings.json стоял
на `Edit|Write|NotebookEdit`. То есть `rm .env`, `cat > .env`,
`git checkout -- .env` не проверялись вовсе — путь, которым .env на каноне
уничтожался трижды, шёл мимо защиты. Тот же класс, за который я разбирал
конфигурацию соседнего агента: правило было записано в одном представлении
входа и не покрывало второе.
"""
import json, os, re, shlex, sys

# Windows: стандартный вывод по умолчанию в кодировке системы (cp1251), и любой
# символ вне её роняет хук с UnicodeEncodeError. Хук падает МОЛЧА — харнесс
# видит ненулевой код возврата и просто не применяет решение, то есть защита
# выглядит установленной, но не работает. Форсируем UTF-8 до первой печати.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


PROTECTED = re.compile(
    r"(^|/)\.env($|\.[^/]*$)"          # .env, .env.local, .env.production, .env.bak
    r"|\.(pem|key|p12|pfx|jks)$"       # ключи и хранилища сертификатов
    r"|(^|/)id_(rsa|ed25519|ecdsa)$"   # приватные SSH-ключи
    r"|(^|/)\.htpasswd$",
    re.I)


def is_protected(path: str) -> bool:
    norm = (path or "").replace("\\", "/").strip()
    if not norm:
        return False
    # .env.example — шаблон без секретов, его править можно
    if norm.endswith(".example"):
        return False
    return bool(PROTECTED.search(norm))


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


WRITE_REASON = (
    "Запись в {p} запрещена: файл содержит секреты. "
    "Новые переменные добавляй в .env.example (шаблон без значений), "
    "а реальные значения владелец вписывает сам.")

# ─── разбор shell-команды ────────────────────────────────────────────────────

SEPARATORS = {";", "&&", "||", "|", "&"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
# Обёртки, за которыми стоит настоящая команда.
WRAPPERS = {"sudo", "doas", "nohup", "time", "command", "exec", "env", "xargs"}
# Аргументы этих команд — все цели разом.
ALL_ARGS = {"rm", "shred", "unlink", "truncate", "tee"}
# У этих цель — последний позиционный аргумент.
LAST_ARG = {"cp", "mv", "ln", "install", "rsync"}
# Признак того, что инлайн-скрипт пишет или удаляет, а не читает.
WRITEY = re.compile(
    r"""open\s*\([^)]*['"][wax]\+?b?['"]"""
    r"""|write_text|writelines|\.write\s*\("""
    r"""|os\.remove|os\.unlink|\.unlink\s*\("""
    r"""|shutil\.(copy|copyfile|copy2|move|rmtree)""")


class Unparsable(Exception):
    """Команду не удалось разобрать — рассуждать о её целях нельзя."""


def _tokenize(cmd: str) -> list[str]:
    """Разбор с уважением к кавычкам: `echo "текст > .env"` — не перенаправление.

    Кавычки здесь несущие в обе стороны. Замер подменой разбора на грубый
    `split()`: 5 настоящих уничтожений перестают ловиться (аргументы в
    кавычках рвутся на куски) и одна законная команда начинает блокироваться
    (`>` внутри текста читается как перенаправление). Поэтому при
    синтаксическом мусоре не «разбираем как получится», а честно сдаёмся
    наверх — там решение принимается в пользу запрета.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError as exc:
        raise Unparsable(str(exc)) from exc


def _git_scan(args: list[str], targets: list[str], blanket: list[str]) -> None:
    sub, rest = None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"-c", "-C", "--git-dir", "--work-tree"}:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        sub, rest = a, args[i + 1:]
        break
    if sub in {"checkout", "restore"}:
        targets.extend(a for a in rest if not a.startswith("-") and a != "--")
    elif sub == "clean":
        # `git clean -fd` игнорируемые файлы НЕ трогает, а .env как раз
        # игнорируемый — блокировать её было бы ложным срабатыванием.
        # Ключ -x снимает ровно это исключение, и .env попадает под снос.
        if any(re.fullmatch(r"-[a-zA-Z]*[xX][a-zA-Z]*", a) for a in rest):
            blanket.append(
                "`git clean -x` удаляет игнорируемые файлы, а .env игнорируемый "
                "по определению. Без -x команда его не трогает — убери ключ или "
                "перечисли пути явно.")


def _inline_scan(args: list[str], targets: list[str]) -> None:
    if "-c" not in args:
        return
    try:
        script = args[args.index("-c") + 1]
    except IndexError:
        return
    if WRITEY.search(script):
        targets.extend(re.findall(r"""['"]([^'"]+)['"]""", script))


def _scan(argv: list[str], targets: list[str], blanket: list[str]) -> None:
    clean: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in REDIRECTS:
            if i + 1 < len(argv):
                targets.append(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        clean.append(tok)
        i += 1

    while clean and (clean[0] in WRAPPERS
                     or (not clean[0].startswith("-") and "=" in clean[0])):
        clean.pop(0)
    if not clean:
        return

    name = os.path.basename(clean[0])
    args = clean[1:]
    plain = [a for a in args if not a.startswith("-")]

    if name in ALL_ARGS:
        targets.extend(plain)
    elif name in LAST_ARG:
        if plain:
            targets.append(plain[-1])
    elif name in {"sed", "perl"} and any(a.startswith("-i") or a == "--in-place"
                                         for a in args):
        targets.extend(plain)
    elif name == "dd":
        targets.extend(a[3:] for a in args if a.startswith("of="))
    elif name == "git":
        _git_scan(args, targets, blanket)
    elif name in {"python", "python3", "py"}:
        _inline_scan(args, targets)


def check_command(cmd: str) -> None:
    targets: list[str] = []
    blanket: list[str] = []
    argv: list[str] = []
    try:
        tokens = _tokenize(cmd)
    except Unparsable:
        # Разобрать нечем, значит и утверждать «цели среди них нет» нечем.
        # Единственная честная реакция — запрет, если секрет вообще упомянут.
        for word in re.split(r"[\s;|&<>'\"]+", cmd):
            if is_protected(word):
                deny("Команду не удалось разобрать (незакрытая кавычка?), а в ней "
                     f"упомянут {word}. Перепиши её однозначно — на неразборчивом "
                     "вводе хук отказывает, а не гадает.")
        return
    for tok in tokens + [";"]:
        if tok in SEPARATORS:
            _scan(argv, targets, blanket)
            argv = []
        else:
            argv.append(tok)

    for path in targets:
        if is_protected(path):
            deny(WRITE_REASON.format(p=path))
    if blanket:
        deny(blanket[0])


# ─── вход ────────────────────────────────────────────────────────────────────

data = json.load(sys.stdin)
tool = data.get("tool_name") or ""
inp = data.get("tool_input") or {}

if tool == "Bash":
    check_command(inp.get("command", "") or "")
else:
    # NotebookEdit кладёт путь в notebook_path, а не в file_path: без второго
    # ключа строка «NotebookEdit» в матчере settings.json обещала защиту,
    # которой не было.
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if is_protected(path):
        deny(WRITE_REASON.format(p=path))

sys.exit(0)
