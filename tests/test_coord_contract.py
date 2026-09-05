"""/coord — межпроектный контракт, а не внутренняя ручка.

По нему кодирует соседняя команда (штаб CRM, tools/shtab.py). 05.09 их CLI
был написан по описанию, а не по коду, и разошёлся в трёх местах из четырёх:
слал `actor` вместо `holder` и `ttl` вместо `ttl_seconds`, ходил в
несуществующий POST /coord/unlock, и не слал обязательный `actor` в квитанции.
Каждый вызов давал бы 422 или 404.

Здесь имена полей и маршруты закреплены. Переименование поля у меня — это
поломка их клиента, и узнать о ней из своего репозитория иначе нечем: их код
в другом проекте, их тесты мои правки не видят.

Отдельно закреплено `extra="forbid"`. Это не строгость ради строгости: именно
она превращает «поле названо иначе» в громкий 422 вместо тихо потерянного
значения — лок с забытым ttl жил бы по умолчанию, и никто бы не заметил.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import coord
from app.api.coord import JournalInput, LockInput, ReceiptInput, is_shared_scope

OWNER = "11111111-1111-1111-1111-111111111111"


# ─── поля тел запросов ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,required,optional", [
    (LockInput, {"resource", "holder"}, {"ttl_seconds", "note"}),
    (ReceiptInput, {"message_id", "actor", "status"}, {"detail"}),
    (JournalInput, {"task", "actor"}, {"result", "files", "tags"}),
])
def test_field_names_are_frozen(model, required, optional):
    fields = set(model.model_fields)
    assert fields == required | optional, (
        f"{model.__name__}: набор полей изменился — клиент соседей сломается молча"
    )
    actual_required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert actual_required == required


@pytest.mark.parametrize("model", [LockInput, ReceiptInput, JournalInput])
def test_unknown_field_is_rejected(model):
    """Лишнее поле обязано давать 422, а не теряться."""
    assert model.model_config.get("extra") == "forbid"


def test_lock_rejects_the_shape_that_was_actually_sent():
    """Тот самый случай: {resource, actor, ttl}."""
    with pytest.raises(Exception) as e:
        LockInput(resource="shtab:1", actor="session:a", ttl=1800)
    text = str(e.value)
    assert "actor" in text and "ttl" in text
    assert "holder" in text, "ошибка обязана называть верное поле, а не только неверное"


def test_receipt_statuses_are_frozen():
    """Статусы поручений соседей ложатся не один в один — граница закреплена."""
    pattern = ReceiptInput.model_fields["status"].metadata[0].pattern
    allowed = set(re.findall(r"[a-z_]+", pattern))
    assert allowed == {"received", "read", "in_progress", "done", "rejected"}
    for theirs in ("queued", "claimed", "waiting_owner", "awaiting_approval",
                   "cancelled", "failed"):
        assert theirs not in allowed, (
            f"{theirs} стал приниматься — отображение на стороне клиента "
            "устарело, а он об этом не узнает"
        )


def test_ttl_bounds_are_frozen():
    md = LockInput.model_fields["ttl_seconds"].metadata
    bounds = {type(m).__name__: getattr(m, "ge", getattr(m, "le", None)) for m in md}
    assert LockInput(resource="r", holder="h").ttl_seconds == 1800
    with pytest.raises(Exception):
        LockInput(resource="r", holder="h", ttl_seconds=29)
    with pytest.raises(Exception):
        LockInput(resource="r", holder="h", ttl_seconds=24 * 3600 + 1)
    assert bounds  # значения границ читаются, а не угадываются


# ─── маршруты ────────────────────────────────────────────────────────────────

def _routes():
    out = set()
    for r in coord.router.routes:
        for m in r.methods:
            out.add((m, r.path))
    return out


def test_routes_are_frozen():
    routes = _routes()
    for method, path in [
        ("POST", "/coord/lock"),
        ("DELETE", "/coord/lock"),      # снятие — параметрами строки запроса
        ("GET", "/coord/locks"),
        ("POST", "/coord/receipt"),
        ("GET", "/coord/receipts/{message_id}"),
        ("POST", "/coord/journal"),
        ("GET", "/coord/journal/search"),
    ]:
        assert (method, path) in routes, f"нет {method} {path}"


def test_unlock_route_does_not_exist():
    """Клиент соседей ходил в POST /coord/unlock и получал 404.

    Проверка держит границу с обеих сторон: если ручку когда-нибудь заведут,
    её надо согласовать, а не обнаружить.
    """
    assert ("POST", "/coord/unlock") not in _routes()


def test_release_takes_query_params_not_body():
    release = next(r for r in coord.router.routes
                   if r.path == "/coord/lock" and "DELETE" in r.methods)
    names = set(release.dependant.query_params and
                [p.name for p in release.dependant.query_params] or [])
    assert {"resource", "holder"} <= names, (
        "снятие лока перестало принимать параметры строки запроса"
    )


# ─── пространство имён ───────────────────────────────────────────────────────

def test_shared_scope_recognises_the_fallback():
    assert is_shared_scope(OWNER) is True
    assert is_shared_scope("agent:session-a") is False


def _request(agent_id="session-a"):
    req = MagicMock()
    req.state.agent_id = agent_id
    return req


def _pool(row):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool


async def test_registered_key_gets_the_shared_namespace():
    with patch.object(coord, "get_pool", AsyncMock(return_value=_pool({"o": OWNER}))):
        owner = await coord._owner_of(_request())
    assert owner == OWNER and is_shared_scope(owner)


async def test_unregistered_key_is_isolated_and_says_so(caplog):
    """Молчаливый откат — главная ловушка этой ручки.

    Ключ, которого нет в agent_keys, даёт своё пространство имён: лок
    берётся, отдаёт 200 и не виден никому. Две сессии «возьмут» один ресурс,
    и обе будут правы. Раньше отличить это от рабочей координации по ответу
    было нельзя.
    """
    coord._warned_isolated.discard("session-b")
    with patch.object(coord, "get_pool", AsyncMock(return_value=_pool(None))), \
         caplog.at_level("WARNING"):
        owner = await coord._owner_of(_request("session-b"))
    assert owner == "agent:session-b"
    assert not is_shared_scope(owner)
    assert any("session-b" in r.getMessage() for r in caplog.records), (
        "откат прошёл без предупреждения в логе"
    )


async def test_warning_is_not_repeated_per_request():
    """/coord/locks опрашивают в цикле — предупреждение не должно стать шумом."""
    coord._warned_isolated.discard("session-c")
    with patch.object(coord, "get_pool", AsyncMock(return_value=_pool(None))), \
         patch.object(coord.logger, "warning") as w:
        await coord._owner_of(_request("session-c"))
        await coord._owner_of(_request("session-c"))
    assert w.call_count == 1


def _redis(*, set_ok=True, keys=()):
    r = MagicMock()
    r.set = AsyncMock(return_value=set_ok)
    r.get = AsyncMock(return_value=None)
    r.ttl = AsyncMock(return_value=1800)

    async def _scan(**kw):
        for k in keys:
            yield k

    r.scan_iter = _scan
    return r


async def _lock_response(owner):
    body = LockInput(resource="shtab:42", holder="session:a", ttl_seconds=1800)
    with patch.object(coord, "verify_api_key", AsyncMock()), \
         patch.object(coord, "_owner_of", AsyncMock(return_value=owner)), \
         patch.object(coord, "get_redis", AsyncMock(return_value=_redis())):
        return await coord.acquire_lock(body, _request())


async def test_lock_answer_says_whether_it_is_a_real_lock():
    """Ради этого поля правка и делалась.

    Раньше ответ на взятие лока в общем и в одиночном пространстве имён был
    побайтно одинаков. Клиент не мог отличить «взял лок» от «взял то, чего
    никто не увидит», и штаб считал бы себя синхронизированным.
    """
    assert (await _lock_response(OWNER))["scope"] == "owner"
    assert (await _lock_response("agent:session-a"))["scope"] == "agent"


async def test_locks_listing_says_it_too():
    """Пустой список неоднозначен: «никто не держит» и «смотрю не туда»."""
    with patch.object(coord, "verify_api_key", AsyncMock()), \
         patch.object(coord, "_owner_of", AsyncMock(return_value="agent:session-a")), \
         patch.object(coord, "get_redis", AsyncMock(return_value=_redis())):
        out = await coord.list_locks(_request())
    assert out["count"] == 0 and out["scope"] == "agent"


async def test_db_failure_also_isolates_and_warns():
    """Недоступная база — тот же исход, и он тоже обязан быть слышен."""
    coord._warned_isolated.discard("session-d")
    with patch.object(coord, "get_pool", AsyncMock(side_effect=RuntimeError("pool"))), \
         patch.object(coord.logger, "warning") as w:
        owner = await coord._owner_of(_request("session-d"))
    assert owner == "agent:session-d"
    assert w.call_count == 1
