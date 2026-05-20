"""FastAPI dependencies для работы с пользовательскими сессиями.

require_user(request)  — обязательно. 401 если нет валидной сессии.
optional_user(request) — опционально. None если нет (для public endpoints).
require_admin(request) — обязательно + проверка accounts.is_admin = TRUE.

Использование:

  from fastapi import Depends
  from app.security.middleware import require_user, Session

  @router.get("/user/me")
  async def get_me(user: Session = Depends(require_user)):
      return {"email": user.email, "user_id": user.user_id}

Кука читается из request.cookies[SESSION_COOKIE_NAME]. Помимо cookie
поддерживается заголовок `X-Session-Id` — удобно для MCP-клиентов и
CLI-инструментов которые не работают с cookie.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from app.security.session import (
    SESSION_COOKIE_NAME,
    Session,
    verify_session,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Внутреннее: извлечь session_id из request (cookie или header)
# ─────────────────────────────────────────────────────────────────────────
def _extract_session_id(request: Request) -> str | None:
    """Берём из cookie SESSION_COOKIE_NAME, fallback на заголовок X-Session-Id."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        return sid
    header_sid = request.headers.get("X-Session-Id")
    return header_sid or None


# ─────────────────────────────────────────────────────────────────────────
# Public dependencies
# ─────────────────────────────────────────────────────────────────────────
async def require_user(request: Request) -> Session:
    """Обязательная аутентификация. 401 если нет валидной сессии."""
    sid = _extract_session_id(request)
    session = await verify_session(sid)
    if not session:
        raise HTTPException(
            status_code=401,
            detail="Не авторизованы. Войдите через ссылку из письма.",
        )
    # Прикрепляем к request.state — удобно для middleware логирования
    request.state.user_id = session.user_id
    request.state.user_email = session.email
    return session


async def optional_user(request: Request) -> Session | None:
    """Опциональная аутентификация. None если нет — но без 401."""
    sid = _extract_session_id(request)
    if not sid:
        return None
    session = await verify_session(sid)
    if session:
        request.state.user_id = session.user_id
        request.state.user_email = session.email
    return session


async def require_admin(request: Request) -> Session:
    """Обязательная аутентификация + проверка is_admin. 403 если не админ."""
    session = await require_user(request)
    if not session.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Нужны права администратора.",
        )
    return session


# ─────────────────────────────────────────────────────────────────────────
# Утилита: извлечь device_info из request (для записи в sessions.device_info)
# ─────────────────────────────────────────────────────────────────────────
def extract_device_info(request: Request) -> dict[str, Any]:
    """Собрать device_info dict для записи при create_session().

    Поля:
      user_agent — короткий, обрезан до 300 символов
      ip         — берётся из X-Forwarded-For (за nginx) или client.host
      accept_lang
      referer    — кто привёл пользователя (если внешняя ссылка)
    """
    ua = (request.headers.get("user-agent") or "")[:300]
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    return {
        "user_agent": ua,
        "ip": ip,
        "accept_lang": request.headers.get("accept-language", "")[:80],
        "referer": (request.headers.get("referer") or "")[:200],
    }
