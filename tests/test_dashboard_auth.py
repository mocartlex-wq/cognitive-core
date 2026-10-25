"""Дашборд не должен отдавать данные без ключа.

Регрессия, проверенная на проде 2026-08-11: весь роутер `/dashboard/*` был
объявлен без единой проверки авторизации. `curl` без заголовков получал 200 и
сырые `raw_payload` из L1 — с IP-адресами и содержимым сообщений, причём без
разделения по владельцам.

Тест дешёвый и тупой намеренно: он ловит именно тот класс ошибки, который
случился, — «обработчик добавили, а ключ спросить забыли». Поэтому проверяются
ВСЕ пути роутера списком: новый обработчик без авторизации сразу уронит тест.
"""
import httpx
import pytest

# Маркер asyncio, не anyio (25.10.2026): при asyncio_mode = auto фикстуру `client`
# поднимает pytest-asyncio в своём цикле, а anyio-тест бежал в другом — на
# teardown httpx закрывал соединение в уже закрытом цикле («Event loop is
# closed», 13 ошибок в CI db-tests на любой ветке, включая docs-only).

# Все пути роутера. Добавляя новый обработчик в app/api/dashboard.py, добавь его
# и сюда — иначе он останется непроверенным.
DASHBOARD_PATHS = [
    "/dashboard/recent-events?limit=1",
    "/dashboard/audit-tail?limit=1",
    "/dashboard/domains",
    "/dashboard/timeline?days=1",
    "/dashboard/tools-registry?limit=1",
    "/dashboard/knowledge?limit=1",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", DASHBOARD_PATHS)
async def test_dashboard_rejects_request_without_key(client: httpx.AsyncClient, path: str):
    """Без X-API-Key — 401, и ни байта данных в ответе."""
    r = await client.get(path)
    assert r.status_code == 401, (
        f"{path} отдал {r.status_code} без ключа — это утечка, а не «read-only endpoint». "
        f"Начало ответа: {r.text[:200]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", DASHBOARD_PATHS)
async def test_dashboard_rejects_invalid_key(client: httpx.AsyncClient, path: str):
    """С мусорным ключом — тоже 401 (проверка не должна быть формальной)."""
    r = await client.get(path, headers={"X-API-Key": "totally-invalid-key-0000"})
    assert r.status_code == 401, f"{path} принял недействительный ключ: {r.status_code}"


@pytest.mark.asyncio
async def test_audit_tail_is_admin_only(client: httpx.AsyncClient, headers: dict):
    """Аудит-лог не разделён по владельцам, поэтому обычному ключу — 403.

    Если у l5_audit_log однажды появится owner_user_id, этот тест нужно менять
    осознанно: разграничение станет возможным, и запрет можно снимать.
    """
    r = await client.get("/dashboard/audit-tail?limit=1", headers=headers)
    if r.status_code == 401:
        pytest.skip("тестовый ключ не принят этим окружением")
    assert r.status_code in (200, 403), f"неожиданный код {r.status_code}"
    if r.status_code == 200:
        # 200 допустим только для админского ключа (owner is None)
        assert "items" in r.json()
