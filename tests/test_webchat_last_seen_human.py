"""Человеческое «последний раз на связи» в чипе «Кому».

Раньше в title уезжал сырой ISO («был(а): 2026-08-10T17:02:43.741856+00:00») —
из него не понять, агент отвалился минуту назад или месяц. humanSince() чистая
и получает now параметром, поэтому её можно прогнать по-настоящему, а не только
проверить глазами по исходнику.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")


def _fn_source() -> str:
    m = re.search(r"function humanSince\(iso, now\)\{.*?\n\}", WEBCHAT, re.S)
    assert m, "humanSince не найдена в webchat.html"
    return m.group(0)


def _run(cases: list[tuple[str | None, str]], tmp_path) -> list[str]:
    """Выполняет humanSince в node на списке (iso, now) и возвращает результаты."""
    harness = _fn_source() + "\nconst out=" + json.dumps([]) + ";\n"
    harness += "const cases=" + json.dumps(cases) + ";\n"
    harness += "for(const [iso, now] of cases){ out.push(humanSince(iso, Date.parse(now))); }\n"
    harness += "console.log(JSON.stringify(out));\n"
    f = tmp_path / "h.mjs"
    f.write_text(harness, encoding="utf-8")
    # encoding задан явно: на Windows text=True декодирует stdout кодировкой
    # консоли (cp1251), и UTF-8 вывод node превращается в мусор.
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


NOW = "2026-09-06T12:00:00Z"


class TestHumanSinceBehaviour:
    def test_minutes_hours_days(self, tmp_path):
        got = _run([
            ("2026-09-06T11:55:00Z", NOW),   # 5 минут
            ("2026-09-06T10:00:00Z", NOW),   # 2 часа
            ("2026-08-10T12:00:00Z", NOW),   # 27 дней
        ], tmp_path)
        assert got == ["был(а) 5 мин назад", "был(а) 2 ч назад", "был(а) 27 дн назад"]

    def test_just_now_under_a_minute(self, tmp_path):
        assert _run([("2026-09-06T11:59:30Z", NOW)], tmp_path) == ["был(а) только что"]

    def test_never_seen(self, tmp_path):
        # last_seen = null после 82e7728: heartbeat сам по себе больше не
        # считается присутствием, так что «никогда» — обычное состояние.
        assert _run([(None, NOW)], tmp_path) == ["не выходил(а) на связь"]

    def test_garbage_is_not_shown_raw(self, tmp_path):
        assert _run([("не-дата", NOW)], tmp_path) == ["не выходил(а) на связь"]

    def test_clock_skew_does_not_produce_negative(self, tmp_path):
        # Часы клиента отстают → отметка «в будущем». Показать «-3 мин назад» хуже,
        # чем «только что».
        got = _run([("2026-09-06T12:03:00Z", NOW)], tmp_path)
        assert got == ["был(а) только что"]
        assert "-" not in got[0]

    def test_boundaries_round_down(self, tmp_path):
        got = _run([
            ("2026-09-06T11:00:00Z", NOW),          # ровно час
            ("2026-09-05T12:00:00Z", NOW),          # ровно сутки
        ], tmp_path)
        assert got == ["был(а) 1 ч назад", "был(а) 1 дн назад"]


class TestWiring:
    def test_chip_uses_helper_not_raw_iso(self):
        assert "humanSince(p.last_seen)" in WEBCHAT
        assert "'был(а): '+p.last_seen" not in WEBCHAT, "сырой ISO не должен попадать в title"

    def test_online_still_wins(self):
        i = WEBCHAT.index("const ttl=p.online")
        assert "'на связи'" in WEBCHAT[i:i + 120]

    def test_now_is_injectable(self):
        # Без параметра now функцию нельзя было бы проверить детерминированно.
        assert "function humanSince(iso, now)" in WEBCHAT
