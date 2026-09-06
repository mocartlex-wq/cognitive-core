"""Композер webchat: голос и вложения с превью (Фаза 1, перенос из room.html).

Ветка claude/composer-mobile-pass-clean делала это для СТАРОГО room.html, который
теперь легаси (/ui/room-legacy). Запись голоса и загрузка через media pipeline к
этому моменту уже жили в webchat.html; недоставало ровно двух вещей: показать
ЧТО прикреплено до отправки (в поле был только маркер «(media:id)») и отсеять
слишком большой файл до base64. Здесь это и закрепляем.

Без БД: проверка по исходнику страницы + серверных констант.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

from app.api import media as media_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")


class TestServerSide:
    def test_webm_allowed_for_audio(self):
        # MediaRecorder в браузере пишет audio/webm; без этого расширения
        # голосовые отбивались сервером. Правка уже на main — держим тестом.
        assert ".webm" in media_mod.ALLOWED_AUDIO_EXT

    def test_audio_cap_matches_ui(self):
        # UI обещает 50 МБ для аудио — цифра должна совпадать с сервером.
        assert media_mod.MAX_AUDIO_SIZE_MB == 50
        assert media_mod.MAX_UPLOAD_SIZE_MB == 200


class TestFilePicker:
    def test_accept_narrows_picker(self):
        # Единственная идея, которой не хватало из ветки: на телефоне accept
        # определяет, что вообще предложат выбрать.
        assert 'id="filePicker" accept="' in WEBCHAT
        for ext in ("image/*", "video/*", "audio/*", ".pdf", ".xlsx"):
            assert ext in WEBCHAT, ext


class TestSizeGuard:
    def test_limits_declared(self):
        assert "MAX_MB=200" in WEBCHAT and "MAX_AUDIO_MB=50" in WEBCHAT

    def test_guard_runs_before_base64(self):
        i_guard = WEBCHAT.index("capMb*1048576")
        i_b64 = WEBCHAT.index("await fileToB64(file)")
        assert i_guard < i_b64, "проверка размера обязана быть до base64"

    def test_audio_detected_by_emoji_or_mime(self):
        assert "emoji==='🎤'" in WEBCHAT
        assert r"/^audio\//.test(file.type" in WEBCHAT


class TestAttachmentPreview:
    def test_bar_exists_and_hidden_by_default(self):
        assert 'id="attBar"' in WEBCHAT
        assert '<div class="att-bar" id="attBar" hidden>' in WEBCHAT

    def test_state_and_render(self):
        for name in ("pendingAtt", "function renderAtt(", "function dropAtt(", "function clearAtt("):
            assert name in WEBCHAT, name

    def test_pushed_after_successful_upload(self):
        i_mid = WEBCHAT.index("media_id||'?'")
        i_push = WEBCHAT.index("pendingAtt.push(")
        assert i_mid < i_push, "вложение попадает в превью только после ответа сервера"

    def test_thumbnail_only_for_images(self):
        # objectURL для 200-мегабайтного видео — лишняя память без пользы.
        assert r"/^image\//.test(file.type" in WEBCHAT
        assert "URL.createObjectURL(file)" in WEBCHAT

    def test_object_urls_revoked(self):
        # Иначе превью течёт: вкладка живёт долго, вложений за сессию много.
        assert WEBCHAT.count("URL.revokeObjectURL") >= 2

    def test_drop_removes_marker_by_media_id(self):
        i = WEBCHAT.index("function dropAtt(")
        body = WEBCHAT[i:i + 500]
        assert "'(media:'+a.mid+')'" in body, "маркер убирается по id, а не по имени файла"

    def test_cleared_after_send(self):
        i_sub = WEBCHAT.index("async function submit(")
        body = WEBCHAT[i_sub:i_sub + 700]
        assert "clearAtt();" in body


class TestExistingContractsKept:
    """Десктоп-пасс, чипы «Кому», presence и ?embed=1 ломать было нельзя."""

    def test_recipients_and_presence(self):
        assert 'id="rcpBar"' in WEBCHAT
        assert "renderRecipients(d && d.participants)" in WEBCHAT
        assert "updatePresence(d && d.participants)" in WEBCHAT

    def test_embed_mode_kept(self):
        assert "embed" in WEBCHAT and "'embed'" in WEBCHAT

    def test_voice_recording_kept(self):
        for t in ("micBtn", "MediaRecorder", "micTimer", "upload_b64"):
            assert t in WEBCHAT, t

    def test_att_bar_sits_above_composer(self):
        # Порядок важен: полоса вложений между чипами «Кому» и полем ввода.
        i_rcp = WEBCHAT.index('id="rcpBar"')
        i_att = WEBCHAT.index('id="attBar"')
        i_comp = WEBCHAT.index('<div class="composer">')
        assert i_rcp < i_att < i_comp


class TestScriptParses:
    def test_inline_script_parses(self, tmp_path):
        body = WEBCHAT.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        f = tmp_path / "t.js"
        # new Function, а не node --check: в скрипте страницы есть return верхнего уровня.
        f.write_text("new Function(" + json.dumps(body) + ");", encoding="utf-8")
        r = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
