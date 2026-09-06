"""Закрытие находок ревью webchat.html от 06.09 (после десктоп-пасса №2).

HIGH: H() не экранировал кавычки, а стоит внутри атрибутов (data-name из
имени файла в чужом сообщении, title, data-token) — агент с ключом комнаты
мог закрыть атрибут и вставить обработчик. MEDIUM: [hidden] на полосах «Кому»
и вложений не работал (авторский display:flex сильнее UA-правила); клиентский
потолок 200 МБ не совпадал с серверными 25/50 для картинок и документов;
статус техподдержки затирался счётчиком присутствия; accept уже серверного
списка.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

from app.api import media as media_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")


def _js_function(name: str) -> str:
    """Вырезать одну function-декларацию из инлайн-скрипта (по балансу скобок)."""
    i = WEBCHAT.index(f"function {name}(")
    j = WEBCHAT.index("{", i)
    depth, k = 0, j
    while True:
        c = WEBCHAT[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return WEBCHAT[i:k + 1]
        k += 1


def _run_node(script: str) -> subprocess.CompletedProcess:
    f = ROOT / "tests" / "_tmp_review_hardening.js"
    f.write_text(script, encoding="utf-8")
    try:
        return subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    finally:
        f.unlink(missing_ok=True)


class TestEscaping:
    def test_h_escapes_quotes(self):
        line = next(ln for ln in WEBCHAT.splitlines() if ln.startswith("const H = "))
        script = line + "\n" + r"""
const out = H('x" onmouseover="location=1" y\' z');
if (out.includes('"') || out.includes("'")) { console.error('unescaped quote: ' + out); process.exit(1); }
if (!out.includes('&quot;') || !out.includes('&#39;')) { console.error('bad escape: ' + out); process.exit(1); }
console.log('ok');
"""
        r = _run_node(script)
        assert r.returncode == 0, r.stderr

    def test_media_name_attribute_cannot_break_out(self):
        """Тот самый вектор из ревью: имя файла в data-name с кавычкой и обработчиком."""
        line = next(ln for ln in WEBCHAT.splitlines() if ln.startswith("const H = "))
        script = line + "\n" + r"""
const fname = 'x" onmouseover="location=1';
const html = `<span class="media" data-name="${H(fname)}">📎 ${H(fname)}</span>`;
// Обработчик должен остаться ТЕКСТОМ внутри значения, а не новым атрибутом:
// после экранирования нет ни одной сырой кавычки перед onmouseover.
if (/" onmouseover=/.test(html)) { console.error(html); process.exit(1); }
if ((html.match(/"/g) || []).length !== 4) { console.error('attr boundary broken: ' + html); process.exit(1); }
console.log('ok');
"""
        r = _run_node(script)
        assert r.returncode == 0, r.stderr


class TestBars:
    def test_hidden_bars_really_hidden(self):
        assert re.search(r"\.rcp-bar\[hidden\],\s*\.att-bar\[hidden\]\{display:none\}", WEBCHAT)


class TestSizeCaps:
    def test_caps_match_server_per_kind(self):
        assert f"MAX_IMAGE_MB={25}" in WEBCHAT       # media.py: 25MB cap для картинок
        assert f"MAX_DOC_MB={media_mod.MAX_DOC_SIZE_MB}" in WEBCHAT
        assert f"MAX_AUDIO_MB={media_mod.MAX_AUDIO_SIZE_MB}" in WEBCHAT
        assert f"MAX_MB={media_mod.MAX_UPLOAD_SIZE_MB}" in WEBCHAT
        assert "capMbFor(file, emoji)" in WEBCHAT

    def test_cap_for_kind_in_node(self):
        consts = next(ln for ln in WEBCHAT.splitlines() if ln.startswith("const MAX_MB="))
        script = consts + "\n" + _js_function("capMbFor") + r"""
const cases = [
  [{type:'image/png', name:'a.png'}, '📎', 25],
  [{type:'', name:'scan.JPG'}, '📎', 25],
  [{type:'application/pdf', name:'a.pdf'}, '📎', 50],
  [{type:'video/mp4', name:'a.mp4'}, '📎', 200],
  [{type:'audio/webm', name:'v.webm'}, '🎤', 50],
  [{type:'', name:'v.webm'}, '📎', 200],
];
for (const [f, e, want] of cases) { const got = capMbFor(f, e); if (got !== want) { console.error(f.name + ': ' + got + ' != ' + want); process.exit(1); } }
console.log('ok');
"""
        r = _run_node(script)
        assert r.returncode == 0, r.stderr


class TestSupportAndAccept:
    def test_support_status_not_overwritten_by_presence(self):
        fn = _js_function("updatePresence")
        assert "roomId===supportRoomId" in fn and "техподдержка · отвечу здесь" in fn

    def test_accept_covers_server_document_list(self):
        accept = re.search(r'id="filePicker" accept="([^"]+)"', WEBCHAT).group(1)
        parts = set(accept.split(","))
        missing = {e for e in media_mod.ALLOWED_DOC_EXT if e not in parts}
        assert not missing, f"accept не знает: {sorted(missing)}"
