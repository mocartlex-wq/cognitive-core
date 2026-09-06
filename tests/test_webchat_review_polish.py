"""Правки по ревью десктоп-пасса: свёртка, статус, подсказка, подписи, скорость.

Ревью-агент прошёлся по правкам 06.09 и нашёл шесть мест. Здесь закреплены
те из них, что относятся к ленте и статусу:

1. потолок свёртки зависит от ширины (на телефоне за той же высотой стоит
   втрое меньше текста) и срабатывает только при заметном превышении;
2. при печати статус не остаётся серым;
3. подсказка «был(а) N назад» не замерзает;
4. время не рисуется дважды на первом сообщении группы;
5. строка с кнопкой «Показать полностью» не считается сообщением;
6. свёртка считает высоту одним проходом, а не после каждой вставки.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")


def _функция(имя: str) -> str:
    """Тело функции из инлайн-скрипта — по балансу скобок."""
    i = WEBCHAT.index(f"function {имя}(")
    начало = WEBCHAT.index("{", i)
    глубина = 0
    for к in range(начало, len(WEBCHAT)):
        if WEBCHAT[к] == "{":
            глубина += 1
        elif WEBCHAT[к] == "}":
            глубина -= 1
            if глубина == 0:
                return WEBCHAT[i: к + 1]
    raise AssertionError(f"не нашла конец функции {имя}")


def _node(код: str) -> str:
    p = subprocess.run(["node", "-e", код], capture_output=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr[:600]
    return p.stdout.strip()


def test_потолок_свёртки_зависит_от_ширины():
    """На телефоне выше: та же высота — втрое меньше текста."""
    код = _функция("потолокСвёртки") + """
    const было=[];
    for(const w of [390, 834, 900, 1440]){
      global.window={innerWidth:w};
      было.push(w+':'+потолокСвёртки());
    }
    console.log(было.join(' '));
    """
    assert _node(код) == "390:480 834:480 900:290 1440:290"


def test_свёртка_только_при_заметном_превышении():
    assert "ЗАПАС_СВЁРТКИ = 60" in WEBCHAT
    тело = _функция("свернутьДлинное")
    assert "потолокСвёртки() + ЗАПАС_СВЁРТКИ" in тело
    # длина осталась запасным путём для скрытой вкладки
    assert "ПОРОГ_СВЁРТКИ" in тело


def test_свёртка_не_повторяется_на_уже_свёрнутом():
    тело = _функция("свернутьДлинное")
    assert "if(пузырь.classList.contains('свёрнут')) return;" in тело


def test_высота_считается_одним_проходом():
    """render только копит строки, считает свёртка очереди — после вставки."""
    рендер = _функция("render")
    assert "ждутСвёртки.push" in рендер
    assert "свернутьДлинное(" not in рендер, "в render не должно быть подсчёта высоты"
    assert "свернутьОчередь();" in _функция("paint")


def test_печать_снимает_серый_статус():
    тело = _функция("applyPresence")
    i = тело.index("typingLabels.length")
    ветка = тело[i: i + 600]
    assert "classList.remove('off')" in ветка, "класс off должен сниматься до выхода"


def test_подсказка_последнего_визита_не_замерзает():
    тело = _функция("renderRecipients")
    assert "p.last_seen" in тело.split("const key=")[1].split("\n")[0]
    # у тех, кто на связи, время в ключ не идёт — иначе бар мигает вхолостую
    assert "p.online?''" in тело


def test_время_не_дублируется_в_шапке_группы():
    блок = WEBCHAT[WEBCHAT.index("if(!mine && !sameSender){"): WEBCHAT.index("log.appendChild(row);")]
    assert "класс='когда'" not in блок
    assert "className='когда'" not in блок
    assert ".who .когда{" not in WEBCHAT
    # а у самого сообщения время осталось
    assert "метка.className='время'" in WEBCHAT


def test_кнопка_показать_полностью_не_сообщение():
    assert "строка-кнопки" in WEBCHAT
    тело = _функция("lastRow")
    assert "строка-кнопки" in тело, "lastRow обязан пропускать строку кнопки"


def test_страница_разбирается():
    куски = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", WEBCHAT, re.S)
    исходник = "\n;\n".join(куски)
    p = subprocess.run(
        ["node", "-e", "const s=require('fs').readFileSync(0,'utf8'); new Function(s);"],
        input=исходник,
        capture_output=True,
        encoding="utf-8",
    )
    assert p.returncode == 0, (p.stderr or "")[:500]
