"""Фаза C веб-приложения /chat: список комнат, service worker, голосовые.

Три правки, у каждой своя измеримая причина.

**Список комнат.** Открытие /chat стоило до 2N запросов при N комнатах: строка
списка тянула полный `/detail` ради одной строки превью, и ещё один проход шёл
в autoOpen — перебором, пока комната не ответит не-403. Теперь превью приходит
вместе со списком.

**Service worker.** Отдаётся с корня: область действия воркера не может быть
шире каталога, из которого он отдан, и из `/static/` он не покрыл бы ни `/chat`,
ни уведомления. Стратегия сетевая намеренно — воркер живёт в браузере до явного
удаления, и кэширующий раньше сети переживёт исправление на сервере.

**Голосовые.** Звук существовал только как расшифровка Whisper: сам файл жил во
временном каталоге и удалялся в finally. Слушать отправленное было нечем, а
расшифровка — не оригинал: имена, цифры и интонацию она теряет молча.
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SW = ROOT / "sandbox" / "sw.js"
WEBCHAT = ROOT / "sandbox" / "webchat.html"


# ─── список комнат ───────────────────────────────────────────────────────────

def _pool_returning(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


def _sql_code_only(sql: str) -> str:
    """SQL без строк-комментариев.

    Первая версия проверки на LATERAL была зелёной и при вырезанном LATERAL:
    слово стояло в комментарии внутри того же запроса, и проверка находила
    объяснение вместо кода. Поймано подменой, а не чтением.
    """
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return " ".join(" ".join(lines).split())


async def _call_my_rooms(rows):
    from app.api import user as api

    pool, conn = _pool_returning(rows)
    who = MagicMock()
    who.user_id = "11111111-1111-1111-1111-111111111111"
    with patch.object(api, "require_user", AsyncMock(return_value=who)), \
         patch.object(api, "get_pool", AsyncMock(return_value=pool)):
        resp = await api.my_rooms(MagicMock())
    return resp, _sql_code_only(conn.fetch.call_args[0][0])


async def test_room_list_carries_preview():
    """Строка списка не должна ходить за карточкой комнаты."""
    at = datetime(2026, 8, 23, 12, 30, tzinfo=timezone.utc)
    resp, _sql = await _call_my_rooms([{
        "id": "r1", "name": "Комната", "created_at": at, "is_owner": True,
        "is_public": True, "last_text": "привет", "last_from": "dsdsd", "last_at": at,
    }])
    item = resp["items"][0]
    assert item["last_message"]["text"] == "привет"
    assert item["last_message"]["from_agent"] == "dsdsd"
    assert item["last_message"]["created_at"].startswith("2026-08-23T12:30")
    assert "last_text" not in item, "сырые колонки наружу не отдаём"


async def test_empty_room_has_no_preview():
    """Комната без сообщений — не пустая строка, а явное отсутствие."""
    resp, _ = await _call_my_rooms([{
        "id": "r1", "name": "Пустая", "created_at": datetime.now(timezone.utc),
        "is_owner": True, "is_public": True,
        "last_text": None, "last_from": None, "last_at": None,
    }])
    assert resp["items"][0]["last_message"] is None


async def test_preview_text_is_truncated():
    """В превью уезжает начало сообщения, а не всё сообщение целиком."""
    at = datetime.now(timezone.utc)
    resp, _ = await _call_my_rooms([{
        "id": "r1", "name": "к", "created_at": at, "is_owner": True,
        "is_public": True, "last_text": "я" * 5000, "last_from": "a", "last_at": at,
    }])
    assert len(resp["items"][0]["last_message"]["text"]) <= 200


async def test_query_takes_last_message_in_one_pass():
    _resp, sql = await _call_my_rooms([])
    assert "LATERAL" in sql, (
        "без LATERAL последнее сообщение берётся отдельным запросом на комнату — "
        "ровно та цена, ради которой правка и делалась"
    )
    assert "SELECT DISTINCT" not in sql, (
        "DISTINCT схлопывал дубли, которые создавал сам JOIN; с LATERAL он "
        "склеивал бы разные последние сообщения"
    )
    assert "EXISTS" in sql


async def test_rooms_are_ordered_by_activity():
    """Список нужен, чтобы найти где ответили, а не что раньше создано."""
    _resp, sql = await _call_my_rooms([])
    assert "ORDER BY COALESCE(m.created_at, r.created_at) DESC" in sql


async def test_missing_rooms_table_still_returns_200():
    """rooms может не существовать (сервис комнат не задеплоен) — не падаем."""
    from app.api import user as api

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception('relation "rooms" does not exist'))

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    who = MagicMock()
    who.user_id = "11111111-1111-1111-1111-111111111111"
    with patch.object(api, "require_user", AsyncMock(return_value=who)), \
         patch.object(api, "get_pool", AsyncMock(return_value=pool)):
        resp = await api.my_rooms(MagicMock())
    assert resp == {"count": 0, "items": []}


# ─── service worker ──────────────────────────────────────────────────────────

async def test_sw_is_served_from_root_with_broad_scope():
    """Из /static/ воркер не покрыл бы /chat: область не шире своего каталога."""
    from app.main import service_worker

    resp = await service_worker()
    assert resp.headers["service-worker-allowed"] == "/"
    assert resp.headers["cache-control"] == "no-store", (
        "браузер кэширует сам файл воркера; без запрета обновление не доедет "
        "до тех, у кого он уже установлен"
    )
    assert str(resp.path).endswith("sw.js")


def test_sw_file_exists():
    assert SW.exists(), "маршрут есть, файла нет — воркер отдаст 404 и не встанет"


def test_sw_never_caches_api_responses():
    """Закэшированный ответ API — вчерашняя переписка без признаков вчерашней."""
    src = SW.read_text(encoding="utf-8")
    guard = src.split("function isData")[1][:300]
    for prefix in ("user", "rooms", "api", "auth"):
        assert prefix in guard, f"{prefix} не исключён из кэширования"


def test_sw_prefers_network_over_cache():
    """Кэш-первым воркер пережил бы исправление на сервере, и чинить его нечем."""
    src = SW.read_text(encoding="utf-8")
    nav = src.split("req.mode === 'navigate'")[1][:500]
    assert "await fetch(req)" in nav
    assert nav.index("await fetch(req)") < nav.index("caches.match"), (
        "в навигации кэш опрошен раньше сети"
    )


def test_sw_activate_purges_old_caches():
    src = SW.read_text(encoding="utf-8")
    assert "caches.delete" in src and "clients.claim" in src


def test_sw_is_registered_by_the_page():
    """Файл и маршрут без регистрации — мёртвый код."""
    assert "navigator.serviceWorker.register('/sw.js'" in WEBCHAT.read_text(encoding="utf-8")


def test_offline_fallback_exists():
    assert (ROOT / "sandbox" / "offline.html").exists()


# ─── голосовые ───────────────────────────────────────────────────────────────

def test_audio_content_types_cover_allowed_extensions():
    """Без верного типа браузер получит octet-stream и <audio> молча не сыграет."""
    from app.api.media import ALLOWED_AUDIO_EXT, AUDIO_CONTENT_TYPE

    missing = ALLOWED_AUDIO_EXT - set(AUDIO_CONTENT_TYPE)
    assert not missing, f"нет типа содержимого для {missing}"


async def test_serving_uses_the_same_type_map_as_upload():
    """Приём и раздача обязаны знать один и тот же набор форматов.

    Сначала карт было две — в приёме и в раздаче, независимые. Подмена
    показала: удаление формата из одной не краснит проверку второй, то есть
    расхождение прошло бы молча и обнаружилось бы как «плеер не играет».
    """
    from app.api import media as api

    obj = MagicMock()
    obj.read = MagicMock(side_effect=[b"x", b""])
    obj.close = MagicMock()
    obj.release_conn = MagicMock()
    s3 = MagicMock()
    s3.get_object = MagicMock(return_value=obj)

    for ext, expected in api.AUDIO_CONTENT_TYPE.items():
        with patch.object(api, "get_s3", MagicMock(return_value=s3)):
            resp = await api.get_frame(f"audio/abc/voice{ext}")
        obj.read = MagicMock(side_effect=[b"x", b""])
        assert resp.media_type == expected, (
            f"{ext}: приём знает {expected}, раздача отдаёт {resp.media_type}"
        )


async def test_frame_serves_audio_with_playable_type():
    """Раздача звука идёт через ту же ручку, что и кадры."""
    from app.api import media as api

    obj = MagicMock()
    obj.read = MagicMock(side_effect=[b"\x1aE\xdf\xa3", b""])
    obj.close = MagicMock()
    obj.release_conn = MagicMock()
    s3 = MagicMock()
    s3.get_object = MagicMock(return_value=obj)
    with patch.object(api, "get_s3", MagicMock(return_value=s3)):
        resp = await api.get_frame("audio/abc123/voice.webm")
    assert resp.media_type == "audio/webm", (
        f"отдали {resp.media_type}: плеер такой ответ не примет"
    )


def test_chat_renders_a_player_not_only_a_transcript():
    src = WEBCHAT.read_text(encoding="utf-8")
    assert "media-audio" in src and "<audio" in src
    # Ветка звука обязана стоять ДО ветки транскрипта: иначе голосовое с
    # расшифровкой снова превратится в текст и плеер не покажется никогда.
    assert src.index("info.kind==='audio'") < src.index("else if(info.transcript)")


def test_upload_keeps_transcript_when_storage_is_down():
    """Расшифровка уже получена — терять её из-за MinIO нельзя."""
    src = io.open(ROOT / "app" / "api" / "media.py", encoding="utf-8").read()
    block = src.split("audio_key = None", 1)[1][:900]
    assert "except Exception" in block, (
        "сохранение звука не обёрнуто — недоступное хранилище уронит и расшифровку"
    )


# ─── превью в интерфейсе ─────────────────────────────────────────────────────

def _code_only(block: str) -> str:
    """Без строк-комментариев: они рассказывают про снятые запросы и сами
    содержат «/detail», из-за чего проверка ловила бы собственное объяснение."""
    return "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("//"))


def test_room_list_no_longer_fetches_detail_per_room():
    src = WEBCHAT.read_text(encoding="utf-8")
    assert "hydrateRoomPreviews" not in src, "догрузка по комнате осталась в коде"
    block = src.split("function renderRoomList")[1].split("async function autoOpen")[0]
    assert "/detail" not in _code_only(block)


def test_autoopen_does_not_probe_rooms():
    src = WEBCHAT.read_text(encoding="utf-8")
    block = src.split("async function autoOpen")[1][:700]
    assert "/detail" not in _code_only(block), "перебор комнат запросами остался"


def test_seen_mark_is_set_where_messages_are_shown():
    """Отметка в openRoom копила бы непрочитанное, пока комната открыта."""
    src = WEBCHAT.read_text(encoding="utf-8")
    paint = src.split("function paint(")[1][:900]
    assert "markSeen(roomId)" in paint


# ─── целостность самих файлов ────────────────────────────────────────────────

FRONT = ["webchat.html", "sw.js", "offline.html", "room.html", "home.html"]


@pytest.mark.parametrize("name", FRONT)
def test_no_stray_control_bytes(name):
    """Управляющий байт в файле — почти всегда испорченное экранирование.

    Поймано на себе 23.08: инструмент записи превратил `\\b` в regexp'е в
    настоящий байт backspace (0x08). Выражение осталось синтаксически верным,
    поэтому ни один из 37 тестов не покраснел, а превью перестало вычищать
    @-адресацию. Тем же путём `\\n` стал переводом строки и порвал regexp
    пополам — вот это уже ломало разбор всей страницы.
    """
    path = ROOT / "sandbox" / name
    if not path.exists():
        pytest.skip(f"{name} отсутствует")
    src = path.read_text(encoding="utf-8")
    bad = {hex(ord(c)) for c in src if ord(c) < 32 and c not in "\r\n\t"}
    assert not bad, f"{name}: управляющие байты {sorted(bad)}"


@pytest.mark.parametrize("name", ["webchat.html", "sw.js"])
def test_javascript_actually_parses(name):
    """Разбор скрипта целиком: единственная проверка, ловящая порванный regexp.

    Без неё синтаксическая ошибка доезжает до прода как белая страница —
    проверять её на глаз можно только после деплоя.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен")
    path = ROOT / "sandbox" / name
    if name.endswith(".js"):
        r = subprocess.run([node, "--check", str(path)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0, r.stderr[:500]
        return

    src = path.read_text(encoding="utf-8")
    body = "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", src, re.S))
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d) / "bundle.js"
        # new Function, а не --check: код страницы содержит return верхнего
        # уровня и вне функции не разбирается.
        tmp.write_text("new Function(" + json.dumps(body) + ");", encoding="utf-8")
        r = subprocess.run([node, str(tmp)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr[:500]
