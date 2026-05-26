"""Endpoints для авторизации по email-коду.

Основной flow (с 2026-05-20):
  POST   /auth/email/request      — выслать 6-значный OTP-код на email
  POST   /auth/email/code/verify  — ввести (email, code), set cookie

Legacy magic-link flow (URL в письме) — оставлен для backward compatibility:
  GET    /auth/email/verify       — подтвердить по клику в письме, set cookie

Сессии:
  POST   /auth/logout             — отозвать текущую сессию
  POST   /auth/logout/all         — отозвать все сессии пользователя
  GET    /auth/sessions           — список активных сессий (мои устройства)
  DELETE /auth/sessions/{id}      — отозвать конкретную сессию

Bootstrap-логика владельца:
  При первом успешном verify под email = OWNER_BOOTSTRAP_EMAIL
  автоматически:
    1. accounts.is_admin = TRUE
    2. UPDATE rooms          SET owner_user_id = NEW.user_id WHERE created_by = 'cogowner-2026'
    3. UPDATE agent_states   SET owner_user_id = NEW.user_id WHERE agent_id IN (<known legacy IDs>)
    4. UPDATE orchestrator_tasks SET owner_user_id = NEW.user_id WHERE user_id = 'owner'

  Адрес владельца задаётся в settings.owner_bootstrap_email (env OWNER_BOOTSTRAP_EMAIL).
  По умолчанию пусто — bootstrap не применяется.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.config import settings
from app.db.postgres import get_pool
from app.security.middleware import (
    extract_device_info,
    require_user,
)
from app.security.session import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
    consume_magic_link_token,
    consume_otp_code,
    create_session,
    issue_otp_code,
    list_active_sessions,
    revoke_all_for_user,
    revoke_session,
)
from app.services.email_client import send_otp_code, send_welcome

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# Дополнительная проверка email — pydantic EmailStr уже хорошо валидирует,
# но дополнительно режем подозрительно длинные адреса и unicode-spoofing
EMAIL_MAX_LEN = 254
EMAIL_LOCAL_PART_RE = re.compile(r"^[A-Za-z0-9._%+\-]+$")


def _normalize_email(email: str) -> str:
    e = email.strip().lower()
    if len(e) > EMAIL_MAX_LEN:
        raise HTTPException(status_code=400, detail="Слишком длинный адрес почты")
    return e


# ─────────────────────────────────────────────────────────────────────────
# Request bodies
# ─────────────────────────────────────────────────────────────────────────
class EmailRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(..., description="Адрес почты для входа")


# ─────────────────────────────────────────────────────────────────────────
# 1. Запросить OTP-код (основной flow с 2026-05-20)
# ─────────────────────────────────────────────────────────────────────────
@router.post("/email/request")
async def request_email_code(body: EmailRequestBody, request: Request):
    """Сгенерировать 6-значный OTP-код и отправить на email.

    Всегда возвращает 200 (даже если email невалидный или rate-limit),
    чтобы не было утечки информации про существование аккаунтов
    (anti-enumeration).

    Под капотом:
      • проверка rate-limit (5/час на адрес)
      • генерация 6-значного кода + SHA-256 хеш в БД
      • отправка письма через email_client (Yandex/Postfix)
    """
    email_norm = _normalize_email(body.email)
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    ua = request.headers.get("user-agent", "")

    code = await issue_otp_code(
        email=email_norm,
        ttl_minutes=settings.magic_link_ttl_minutes or 15,
        ip_address=ip,
        user_agent=ua,
    )

    if not code:
        logger.info("otp_request_throttled email=%s ip=%s", email_norm, ip)
        return {"ok": True, "sent": False, "throttled": True}

    result = await send_otp_code(
        email=email_norm,
        code=code,
        ttl_minutes=settings.magic_link_ttl_minutes or 15,
        ip_address=ip,
        user_agent=ua,
    )

    if not result.success:
        logger.warning(
            "otp_send_failed email=%s err=%s mid=%s",
            email_norm, result.error, result.message_id,
        )

    return {
        "ok": True,
        "sent": result.success,
        "message_id": result.message_id if result.success else None,
    }


# ─────────────────────────────────────────────────────────────────────────
# 2. Проверить OTP-код (вход)
# ─────────────────────────────────────────────────────────────────────────
class CodeVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    code: str = Field(..., min_length=4, max_length=20)


@router.post("/email/code/verify")
async def verify_email_code(body: CodeVerifyBody, request: Request, response: Response):
    """Принять (email, code), создать сессию, set-cookie.

    Возвращает JSON (НЕ редирект — это API endpoint для AJAX-формы
    из /ui/login). Браузерный JS сам делает location.href = '/ui/profile'
    после успешного ответа.
    """
    email_in = _normalize_email(body.email)
    code = body.code.strip()

    verified_email = await consume_otp_code(email_in, code)
    if not verified_email:
        raise HTTPException(
            status_code=401,
            detail="Неверный или истёкший код. Запросите новый.",
        )

    # Найти/создать accounts row
    pool = await get_pool()
    is_new_account = False
    is_owner_bootstrap = False

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT user_id::text AS user_id, is_admin FROM accounts "
            "WHERE email = $1 AND deleted_at IS NULL",
            verified_email,
        )
        if existing:
            user_id = existing["user_id"]
            await conn.execute(
                "UPDATE accounts SET email_verified = TRUE, last_login_at = NOW() "
                "WHERE user_id = $1::uuid",
                user_id,
            )
        else:
            is_new_account = True
            row = await conn.fetchrow(
                """
                INSERT INTO accounts (email, email_verified, last_login_at)
                VALUES ($1, TRUE, NOW())
                RETURNING user_id::text AS user_id
                """,
                verified_email,
            )
            user_id = row["user_id"]

            bootstrap_email = (settings.owner_bootstrap_email or "").strip().lower()
            if bootstrap_email and verified_email == bootstrap_email:
                is_owner_bootstrap = True
                await _apply_owner_bootstrap(conn, user_id)

    device_info = extract_device_info(request)
    session_id, expires_at = await create_session(user_id=user_id, device_info=device_info)
    _set_session_cookie(response, session_id, expires_at)

    if is_new_account:
        try:
            await send_welcome(email=verified_email, is_owner=is_owner_bootstrap)
        except Exception as e:
            logger.warning("welcome_email_failed email=%s err=%s", verified_email, e)

        # Phase 5C: auto-create Gitea org для нового tenant'а.
        # Best-effort — если Gitea не deployed или down, не блокируем regstration.
        try:
            from app.services.gitea_client import ensure_org_for_owner
            gitea_result = await ensure_org_for_owner(verified_email)
            logger.info("gitea ensure_org email=%s ok=%s", verified_email, gitea_result.get("ok"))
        except Exception as e:
            logger.warning("gitea_org_create_failed email=%s err=%s", verified_email, e)

    return {
        "ok": True,
        "user_id": user_id,
        "email": verified_email,
        "is_new": is_new_account,
        "session_expires_at": expires_at.isoformat(),
        # Phase 5C: hint UI для редиректа новичков на /ui/welcome
        "redirect_to": "/ui/welcome" if is_new_account else "/ui/profile",
    }


# ─────────────────────────────────────────────────────────────────────────
# 3. (Legacy) Подтвердить magic-link URL (клик из письма) — для старых писем
# ─────────────────────────────────────────────────────────────────────────
@router.get("/email/verify")
async def verify_magic_link(token: str, request: Request, response: Response):
    """Принять токен из URL, проверить, создать сессию, set-cookie, redirect.

    Если редирект-цель в settings.app_url настроена — редиректит на
    {app_url}/ui/profile с set-cookie. Иначе возвращает JSON.
    """
    if not token or len(token) < 16 or len(token) > 200:
        raise HTTPException(status_code=400, detail="Некорректная ссылка")

    email = await consume_magic_link_token(token)
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Ссылка устарела или уже использована. Запросите новую.",
        )

    # Найти/создать accounts row
    pool = await get_pool()
    is_new_account = False
    is_owner_bootstrap = False

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT user_id::text AS user_id, is_admin FROM accounts "
            "WHERE email = $1 AND deleted_at IS NULL",
            email,
        )
        if existing:
            user_id = existing["user_id"]
            await conn.execute(
                "UPDATE accounts SET email_verified = TRUE, last_login_at = NOW() "
                "WHERE user_id = $1::uuid",
                user_id,
            )
        else:
            is_new_account = True
            row = await conn.fetchrow(
                """
                INSERT INTO accounts (email, email_verified, last_login_at)
                VALUES ($1, TRUE, NOW())
                RETURNING user_id::text AS user_id
                """,
                email,
            )
            user_id = row["user_id"]

            # Bootstrap-владелец: первое создание под OWNER_BOOTSTRAP_EMAIL
            bootstrap_email = (settings.owner_bootstrap_email or "").strip().lower()
            if bootstrap_email and email == bootstrap_email:
                is_owner_bootstrap = True
                await _apply_owner_bootstrap(conn, user_id)

    # Создать сессию + set-cookie
    device_info = extract_device_info(request)
    session_id, expires_at = await create_session(
        user_id=user_id, device_info=device_info,
    )
    _set_session_cookie(response, session_id, expires_at)

    # Welcome-email если первый раз
    if is_new_account:
        try:
            await send_welcome(email=email, is_owner=is_owner_bootstrap)
        except Exception as e:
            logger.warning("welcome_email_failed email=%s err=%s", email, e)

    # Куда редиректить
    base = (settings.app_url or "").rstrip("/")
    if base:
        redirect = RedirectResponse(url=f"{base}/ui/profile", status_code=303)
        _set_session_cookie(redirect, session_id, expires_at)
        return redirect

    return {
        "ok": True,
        "user_id": user_id,
        "email": email,
        "is_new": is_new_account,
        "session_expires_at": expires_at.isoformat(),
    }


async def _apply_owner_bootstrap(conn, user_id: str) -> None:
    """Привязать существующие legacy-объекты к владельцу при первом входе.

    Выполняется один раз, при первом успешном verify под OWNER_BOOTSTRAP_EMAIL.
    """
    logger.info("owner_bootstrap_apply user_id=%s", user_id)

    # 1. Помечаем админом + display_name
    await conn.execute(
        """
        UPDATE accounts
           SET is_admin = TRUE,
               display_name = COALESCE(display_name, 'Owner')
         WHERE user_id = $1::uuid
        """,
        user_id,
    )

    # 2. Все комнаты, созданные под cogowner-2026 — теперь его
    try:
        rooms_count = await conn.fetchval(
            """
            WITH u AS (
                UPDATE rooms
                   SET owner_user_id = $1::uuid
                 WHERE created_by = 'cogowner-2026' AND owner_user_id IS NULL
                RETURNING 1
            ) SELECT COUNT(*) FROM u
            """,
            user_id,
        )
        logger.info("owner_bootstrap rooms_migrated=%d", rooms_count or 0)
    except Exception as e:
        # rooms таблица может ещё не существовать или иметь другую структуру
        logger.warning("owner_bootstrap rooms_skip err=%s", e)

    # 3. Legacy агенты владельца
    legacy_agents = ("cognitive-core-laptop", "ai-crm-deploy", "orchestrator-bot", "agent_designer")
    try:
        agents_count = await conn.fetchval(
            """
            WITH u AS (
                UPDATE agent_states
                   SET owner_user_id = $1::uuid
                 WHERE agent_id = ANY($2::text[]) AND owner_user_id IS NULL
                RETURNING 1
            ) SELECT COUNT(*) FROM u
            """,
            user_id, list(legacy_agents),
        )
        logger.info("owner_bootstrap agents_migrated=%d", agents_count or 0)
    except Exception as e:
        logger.warning("owner_bootstrap agents_skip err=%s", e)

    # 4. Orchestrator tasks с user_id='owner'
    try:
        await conn.execute(
            """
            UPDATE orchestrator_tasks
               SET owner_user_id = $1::uuid
             WHERE owner_user_id IS NULL
               AND (session_id IS NULL OR session_id = 'owner')
            """,
            user_id,
        )
    except Exception as e:
        logger.warning("owner_bootstrap tasks_skip err=%s", e)


# ─────────────────────────────────────────────────────────────────────────
# 2b. /auth/status — состояние сессии БЕЗ 401 (для auth-widget)
# ─────────────────────────────────────────────────────────────────────────
@router.get("/status")
async def auth_status(request: Request):
    """Проверка состояния аутентификации без выброса 401.

    Используется auth-widget'ом в top-bar — ему нужно знать «залогинен или
    нет», а не ловить ошибку 401 (которая засоряет browser console).
    Возвращает 200 в обоих случаях:

      {"authenticated": false}                     — не залогинен
      {"authenticated": true, "email": ..., ...}   — залогинен

    Безопасность: не выдаём sensitive данные (password_hash, session_id и т.п.).
    """
    from app.security.session import verify_session

    sid = request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("X-Session-Id")
    if not sid:
        return {"authenticated": False}

    session = await verify_session(sid)
    if not session:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": session.email,
        "display_name": session.display_name,
        "is_admin": session.is_admin,
        "user_id": session.user_id,
    }


# ─────────────────────────────────────────────────────────────────────────
# 3. Logout
# ─────────────────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(request: Request, response: Response):
    """Отозвать текущую сессию + очистить cookie."""
    sid = request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("X-Session-Id")
    if sid:
        await revoke_session(sid)
    _clear_session_cookie(response)
    return {"ok": True}


@router.post("/logout/all")
async def logout_all(request: Request, response: Response):
    """Отозвать ВСЕ сессии текущего пользователя (logout с всех устройств)."""
    from app.security.middleware import require_user  # local — avoid cycle при импорте
    user = await require_user(request)
    revoked = await revoke_all_for_user(user.user_id)
    _clear_session_cookie(response)
    return {"ok": True, "revoked": revoked}


# ─────────────────────────────────────────────────────────────────────────
# 4. Список устройств / отзыв конкретной сессии
# ─────────────────────────────────────────────────────────────────────────
@router.get("/sessions")
async def my_sessions(request: Request):
    user = await require_user(request)
    current_sid = request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("X-Session-Id")
    items = await list_active_sessions(user.user_id)
    for it in items:
        it["is_current"] = (it.get("session_id") == current_sid)
        # Подсветка короткого id для UI («…abc123»)
        sid = it.get("session_id") or ""
        it["short_id"] = sid[-8:] if sid else ""
        # Сериализуем datetime в ISO для JSON
        for k in ("created_at", "last_used_at", "expires_at"):
            v = it.get(k)
            if isinstance(v, datetime):
                it[k] = v.isoformat()
    return {"count": len(items), "items": items}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    user = await require_user(request)
    # Безопасность: убедиться что сессия принадлежит текущему пользователю
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT user_id::text FROM sessions WHERE session_id = $1",
            session_id,
        )
    if not owner or owner != user.user_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    revoked = await revoke_session(session_id)
    return {"ok": True, "revoked": revoked}


# ─────────────────────────────────────────────────────────────────────────
# Cookie helpers
# ─────────────────────────────────────────────────────────────────────────
def _set_session_cookie(response: Response, session_id: str, expires_at: datetime) -> None:
    """HTTP-only, Secure, SameSite=Lax (Lax — чтобы magic-link редирект
    из почтового клиента работал)."""
    max_age = SESSION_TTL_DAYS * 86400
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=max_age,
        expires=expires_at,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
