"""Витрина чата на большом экране: кто, когда и сколько текста.

Владелец посмотрел /chat на 1920×1080: «как неудобно организовано, жуть».
Разбор скриншота дал четыре вещи, и все четыре — про большой экран:

1. непонятно, КТО написал: имя 11 точками серым читалось как подпись;
2. непонятно, КОГДА: время стояло только у разделителя дня;
3. отчёты агентов шли стенами — свёртка решала по длине текста, а в широкой
   колонке те же 1200 знаков занимают вдвое меньше строк, чем на телефоне;
4. лента растягивалась на всю панель (строка за 140 знаков), а по бокам
   приложения оставалось по 240 точек чёрного поля.

Проверяем по исходнику страницы, без сервера, — как в соседних тестах.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")


def test_имя_отправителя_читаемого_размера():
    """13 точек и полужирным, цветом текста, а не приглушённым."""
    m = re.search(r"\.who\{([^}]*)\}", WEBCHAT)
    assert m, "нет правила .who"
    правило = m.group(1)
    assert "font-size:13px" in правило
    assert "font-weight:600" in правило
    assert "color:var(--ink)" in правило


def test_время_у_каждого_сообщения():
    """Метка времени в строке сообщения, а не только у разделителя дня."""
    assert ".время{" in WEBCHAT
    assert "метка.className='время'" in WEBCHAT
    assert "function чч_мм(" in WEBCHAT
    # у своих сообщений время уходит влево от пузыря
    assert ".row.me .время{order:-1}" in WEBCHAT


def test_имя_один_раз_на_группу_а_время_у_каждого():
    """Имя рисуется только когда отправитель сменился; время — всегда."""
    блок = WEBCHAT[WEBCHAT.index("const sameSender"): WEBCHAT.index("log.appendChild(row);")]
    assert "if(!mine && !sameSender){" in блок          # имя — по смене отправителя
    assert "метка.className='время'" in блок            # время — вне этого условия
    условие = блок.index("if(!mine && !sameSender){")
    время = блок.index("метка.className='время'")
    assert время > условие, "время должно ставиться вне блока имени"


def test_свёртка_решает_по_высоте_а_не_по_длине():
    assert "ПОТОЛОК_ВЫСОТЫ" in WEBCHAT
    блок = WEBCHAT[WEBCHAT.index("function свернутьДлинное"):][:900]
    assert "scrollHeight" in блок
    # длина осталась запасным признаком — в скрытой вкладке высота нулевая
    assert "ПОРОГ_СВЁРТКИ" in блок


def test_читаемая_колонка_и_композер_по_ней_же():
    """Лента и поле ввода держат одну колонку в 900 точек."""
    for селектор in ("#log{padding:16px max(20px", ".composer{padding:10px max(16px"):
        assert селектор in WEBCHAT, селектор
    assert WEBCHAT.count("calc((100% - 900px) / 2)") >= 4


def test_широкий_экран_занят():
    """От 1600 панель шире 1440, список комнат — 360."""
    m = re.search(r"@media \(min-width: 1600px\)\{([^}]*\{[^}]*\})", WEBCHAT)
    assert m, "нет правила для широкого экрана"
    assert "max-width:1680px" in m.group(1)
    assert "grid-template-columns:360px 1fr" in m.group(1)


def test_поля_по_бокам_не_чёрный_провал():
    assert "radial-gradient(120% 100% at 50% 0%, #131317 0%, #000 70%)" in WEBCHAT
    # светлая тема: класс стоит на .app, фон страницы ловим через :has
    assert "html:has(.app.light) body" in WEBCHAT


def test_мобильный_вид_не_тронут():
    """Всё десктопное — внутри медиа-запросов; телефон видит прежнюю ленту."""
    for правило in ("#log{padding:16px max(20px", ".composer{padding:10px max(16px"):
        i = WEBCHAT.index(правило)
        начало = WEBCHAT.rindex("@media", 0, i)
        assert "min-width: 900px" in WEBCHAT[начало:начало + 40], правило


def test_страница_разбирается():
    """JS страницы синтаксически цел (иначе чат не откроется вовсе)."""
    куски = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", WEBCHAT, re.S)
    assert куски, "на странице нет скриптов"
    исходник = "\n;\n".join(куски)
    proc = subprocess.run(
        ["node", "-e", "const s=require('fs').readFileSync(0,'utf8'); new Function(s);"],
        input=исходник.encode("utf-8"),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:500]
