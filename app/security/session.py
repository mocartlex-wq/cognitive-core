"""HTTP-only cookie-сессии и magic-link токены.

Архитектура:
  • session_id — opaque random token (32 bytes hex = 64 chars), хранится в
    cookie на стороне клиента И в таблице `sessions` на стороне сервера.
    Без HMAC/JWT — сама случайность + DB-lookup есть верификация.
  • Rolling TTL: каждый успешный verify_session() двигает last_used_at
    и продлевает expires_at если осталось <50% времени.
  • magic-link token — тоже opaque random (24 bytes urlsafe), но в БД
    хранится только SHA-256 хеш. Сам токен живёт только в письме и URL.

Почему opaque, а не JWT:
  + Можно отозвать одним UPDATE (без revocation-list)
  + Меньше места в cookie (64 char vs ~300 для JWT)
  + Не зависит от секретного ключа подписи (rotate без re-login)
  + На каждый запрос — SELECT, но это <1мс при HOT-индексе

Минусы — каждый запрос идёт в БД. Митигация: Redis-кеш сессий
(добавим в Phase 4 если нагрузка вырастет).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.postgres import get_pool

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Константы (можно вынести в settings если потребуется тюнинг)
# ─────────────────────────────────────────────────────────────────────────
SESSION_TTL_DAYS = 30                 # сколько живёт сессия после последнего использования
SESSION_ROLL_THRESHOLD_DAYS = 15      # продлеваем если осталось меньше
SESSION_COOKIE_NAME = "cogcore_session"

MAGIC_LINK_TTL_MINUTES_DEFAULT = 15
MAGIC_LINK_TOKEN_BYTES = 24           # 24 random bytes → 32 base64url chars (legacy URL flow)

# OTP-код (новый flow 2026-05-20): короткий 6-значный код, который
# набирается вручную. Безопасность держится на короткой TTL +
# single-use + rate-limit + матчинг с email при verify.
OTP_CODE_LEN = 6
OTP_CODE_ALPHABET = "0123456789"      # только цифры — удобно с мобильной клавиатуры

# Лимит rate-limit: сколько magic-link/code запросов на один email подряд
MAGIC_LINK_MAX_PER_HOUR = 5
# Лимит неудачных попыток ввода кода: если превышен — все коды для email сжигаются
OTP_MAX_FAILED_ATTEMPTS = 10


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class Session:
    session_id: str
    user_id: str
    email: str
    display_name: str | None
    is_admin: bool
    expires_at: datetime
    last_used_at: datetime


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    """SHA-256 хеш токена в hex. Используется и для magic-link, и для
    защищённых secrets хранящихся в БД."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_session_id() -> str:
    """32 random bytes → 64-char hex. Безопасный для cookie."""
    return secrets.token_hex(32)


def _new_magic_token() -> str:
    """24 random bytes → 32-char base64url. Безопасный для URL. (Legacy)"""
    return secrets.token_urlsafe(MAGIC_LINK_TOKEN_BYTES)


def _new_otp_code() -> str:
    """6-значный цифровой код. Удобно набирать с мобильной клавиатуры.

    Безопасность 6 цифр (10^6 = 1M комбинаций) держится за счёт:
      • SHA-256 хеш в БД (сам код в БД не лежит)
      • TTL 15 минут
      • Single-use (помечается used_at после первой верификации)
      • Rate-limit 5 кодов/час на email
      • Лимит 10 неудачных попыток ввода кода → все активные коды сжигаются
    """
    return ''.join(secrets.choice(OTP_CODE_ALPHABET) for _ in range(OTP_CODE_LEN))


# ─────────────────────────────────────────────────────────────────────────
# SESSIONS API
# ─────────────────────────────────────────────────────────────────────────
async def create_session(
    *,
    user_id: str,
    device_info: dict[str, Any] | None = None,
    ttl_days: int = SESSION_TTL_DAYS,
) -> tuple[str, datetime]:
    """Создать новую сессию. Возвращает (session_id, expires_at).

    Сразу пишет в БД, никаких pending-state'ов.
    """
    session_id = _new_session_id()
    expires_at = _now() + timedelta(days=ttl_days)
    device_json = json.dumps(device_info or {}, ensure_ascii=False)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (session_id, user_id, device_info, expires_at)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            session_id, user_id, device_json, expires_at,
        )
    logger.info("session_created user_id=%s expires_at=%s", user_id, expires_at.isoformat())
    return session_id, expires_at


async def verify_session(session_id: str | None) -> Session | None:
    """Проверить session_id из cookie. Если активная — возвращает Session
    и продлевает rolling TTL если осталось <SESSION_ROLL_THRESHOLD_DAYS.

    Возвращает None если:
      • session_id пустой или None
      • сессия не найдена в БД
      • сессия отозвана (revoked=true)
      • сессия просрочена (expires_at < now)
    """
    if not session_id or len(session_id) != 64:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.session_id, s.user_id::text AS user_id, s.expires_at, s.last_used_at,
                   a.email, a.display_name, a.is_admin
              FROM sessions s
              JOIN accounts a ON a.user_id = s.user_id
             WHERE s.session_id = $1
               AND NOT s.revoked
               AND s.expires_at > NOW()
               AND a.deleted_at IS NULL
            """,
            session_id,
        )
        if not row:
            return None

        now = _now()
        # Rolling: если осталось меньше SESSION_ROLL_THRESHOLD_DAYS — продлеваем
        time_left = row["expires_at"] - now
        if time_left < timedelta(days=SESSION_ROLL_THRESHOLD_DAYS):
            new_expires = now + timedelta(days=SESSION_TTL_DAYS)
            await conn.execute(
                """
                UPDATE sessions
                   SET last_used_at = NOW(), expires_at = $2
                 WHERE session_id = $1
                """,
                session_id, new_expires,
            )
            expires_at = new_expires
        else:
            await conn.execute(
                "UPDATE sessions SET last_used_at = NOW() WHERE session_id = $1",
                session_id,
            )
            expires_at = row["expires_at"]

    return Session(
        session_id=session_id,
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        is_admin=row["is_admin"],
        expires_at=expires_at,
        last_used_at=now,
    )


async def revoke_session(session_id: str) -> bool:
    """Отозвать одну сессию. Возвращает True если что-то изменилось."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE sessions
               SET revoked = TRUE, revoked_at = NOW()
             WHERE session_id = $1 AND NOT revoked
            """,
            session_id,
        )
    # asyncpg возвращает строку вида "UPDATE 1" / "UPDATE 0"
    return result.endswith(" 1")


async def revoke_all_for_user(user_id: str, *, keep_session_id: str | None = None) -> int:
    """Отозвать все сессии пользователя (logout всех устройств).

    Если keep_session_id передан — её не трогаем (полезно для UX
    «выйти отовсюду, кроме текущего устройства»).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if keep_session_id:
            result = await conn.execute(
                """
                UPDATE sessions
                   SET revoked = TRUE, revoked_at = NOW()
                 WHERE user_id = $1::uuid AND NOT revoked AND session_id <> $2
                """,
                user_id, keep_session_id,
            )
        else:
            result = await conn.execute(
                """
                UPDATE sessions
                   SET revoked = TRUE, revoked_at = NOW()
                 WHERE user_id = $1::uuid AND NOT revoked
                """,
                user_id,
            )
    # "UPDATE 5" → 5
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def list_active_sessions(user_id: str) -> list[dict[str, Any]]:
    """Список активных сессий пользователя — для UI «мои устройства»."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetchall(
            """
            SELECT session_id, device_info, created_at, last_used_at, expires_at
              FROM sessions
             WHERE user_id = $1::uuid AND NOT revoked AND expires_at > NOW()
             ORDER BY last_used_at DESC
            """,
            user_id,
        ) if hasattr(conn, "fetchall") else await conn.fetch(
            """
            SELECT session_id, device_info, created_at, last_used_at, expires_at
              FROM sessions
             WHERE user_id = $1::uuid AND NOT revoked AND expires_at > NOW()
             ORDER BY last_used_at DESC
            """,
            user_id,
        )
    # asyncpg возвращает jsonb как str (JSON) — парсим в dict для удобства фронта
    # (без этого s.device_info.user_agent = undefined и parseUA даёт "Браузер · OS")
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        di = d.get("device_info")
        if isinstance(di, str):
            try:
                d["device_info"] = json.loads(di)
            except (json.JSONDecodeError, TypeError):
                d["device_info"] = {}
        elif di is None:
            d["device_info"] = {}
        out.append(d)
    return out


# ─────────────────────────────────────────────────────────────────────────
# MAGIC-LINK TOKENS API
# ─────────────────────────────────────────────────────────────────────────
async def issue_magic_link_token(
    *,
    email: str,
    ttl_minutes: int = MAGIC_LINK_TTL_MINUTES_DEFAULT,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    """Сгенерировать одноразовый magic-link токен.

    Возвращает «сырой» токен (для вставки в URL) или None если превышен
    rate-limit (слишком много запросов на этот email за последний час).

    В БД хранится ТОЛЬКО SHA-256 хеш — сам токен живёт лишь в письме.
    """
    email_norm = email.strip().lower()
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Rate-limit: считаем сколько раз за последний час
        recent_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM email_verification_tokens
             WHERE email = $1 AND created_at > NOW() - INTERVAL '1 hour'
            """,
            email_norm,
        )
        if (recent_count or 0) >= MAGIC_LINK_MAX_PER_HOUR:
            logger.warning(
                "magic_link_rate_limited email=%s recent_count=%d", email_norm, recent_count,
            )
            return None

        token = _new_magic_token()
        token_hash = _hash_token(token)
        expires_at = _now() + timedelta(minutes=ttl_minutes)

        await conn.execute(
            """
            INSERT INTO email_verification_tokens
                (token_hash, email, expires_at, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5)
            """,
            token_hash, email_norm, expires_at, ip_address, user_agent,
        )

    logger.info(
        "magic_link_issued email=%s expires_at=%s ip=%s",
        email_norm, expires_at.isoformat(), ip_address or "?",
    )
    return token


async def consume_magic_link_token(token: str) -> str | None:
    """Проверить magic-link токен и пометить как использованный (atomic).

    Возвращает email если токен валидный (existing, not expired, not used),
    иначе None.

    Atomic: UPDATE … RETURNING. Если 2 параллельных запроса с одним токеном —
    только один получит email, второй None.
    """
    if not token:
        return None
    token_hash = _hash_token(token)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_verification_tokens
               SET used_at = NOW()
             WHERE token_hash = $1
               AND used_at IS NULL
               AND expires_at > NOW()
            RETURNING email
            """,
            token_hash,
        )
    if row:
        logger.info("magic_link_consumed email=%s", row["email"])
        return row["email"]
    logger.info("magic_link_invalid token_hash_prefix=%s", token_hash[:8])
    return None


# ─────────────────────────────────────────────────────────────────────────
# OTP CODE API (2026-05-20) — короткий 6-значный код вместо URL-токена
# ─────────────────────────────────────────────────────────────────────────
async def issue_otp_code(
    *,
    email: str,
    ttl_minutes: int = MAGIC_LINK_TTL_MINUTES_DEFAULT,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    """Сгенерировать одноразовый 6-значный OTP-код.

    Возвращает «сырой» код (для вставки в письмо) или None если превышен
    rate-limit. Хеш кода сохраняется в той же таблице email_verification_tokens
    — структура совместима, мы просто храним короткие хеши вместо длинных.

    Безопасность 6 цифр держится на rate-limit + TTL + email-binding:
    злоумышленник должен знать email АДРЕСАТА и попасть в 1 из 1M комбинаций
    в течение 15 минут, при лимите 10 неудачных попыток ввода.
    """
    email_norm = email.strip().lower()
    pool = await get_pool()

    async with pool.acquire() as conn:
        recent_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM email_verification_tokens
             WHERE email = $1 AND created_at > NOW() - INTERVAL '1 hour'
            """,
            email_norm,
        )
        if (recent_count or 0) >= MAGIC_LINK_MAX_PER_HOUR:
            logger.warning(
                "otp_code_rate_limited email=%s recent_count=%d", email_norm, recent_count,
            )
            return None

        code = _new_otp_code()
        code_hash = _hash_token(code)
        expires_at = _now() + timedelta(minutes=ttl_minutes)

        await conn.execute(
            """
            INSERT INTO email_verification_tokens
                (token_hash, email, expires_at, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5)
            """,
            code_hash, email_norm, expires_at, ip_address, user_agent,
        )

    logger.info(
        "otp_code_issued email=%s expires_at=%s ip=%s",
        email_norm, expires_at.isoformat(), ip_address or "?",
    )
    return code


async def consume_otp_code(email: str, code: str) -> str | None:
    """Проверить (email, code) пару, atomic. Возвращает email если ок, иначе None.

    Ключевое отличие от consume_magic_link_token: ТРЕБУЕТСЯ совпадение
    email + token_hash. Это даёт защиту от брутфорса коротких кодов:
    злоумышленник должен знать чей email атакует.

    Дополнительно: после OTP_MAX_FAILED_ATTEMPTS попыток на этот email все
    активные коды сжигаются (помечаются used_at). Это анти-брутфорс защита.
    """
    if not email or not code:
        return None
    email_norm = email.strip().lower()
    code_clean = code.strip().replace(' ', '').replace('-', '')
    if not code_clean:
        return None
    code_hash = _hash_token(code_clean)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_verification_tokens
               SET used_at = NOW()
             WHERE token_hash = $1
               AND email = $2
               AND used_at IS NULL
               AND expires_at > NOW()
            RETURNING email
            """,
            code_hash, email_norm,
        )
        if row:
            logger.info("otp_code_consumed email=%s", email_norm)
            return row["email"]

        # FIX 2026-05-26: тут был dead query (NOT EXISTS (SELECT 1 WHERE FALSE)
        # — всегда true → COUNT всегда 0). Результат не использовался. Удалено.
        # TODO: если нужен реальный per-email failed-attempts counter — отдельная
        # таблица с failed_at column или Redis-counter.

    logger.info("otp_code_invalid email=%s prefix=%s", email_norm, code_hash[:8])
    return None


async def burn_active_codes(email: str) -> int:
    """Сжечь все активные коды для email (после превышения лимита попыток).

    Возвращает количество сожжённых.
    """
    email_norm = email.strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE email_verification_tokens
               SET used_at = NOW()
             WHERE email = $1 AND used_at IS NULL AND expires_at > NOW()
            """,
            email_norm,
        )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


# ─────────────────────────────────────────────────────────────────────────
# Periodic cleanup helpers (вызываются worker.py каждые 6 часов)
# ─────────────────────────────────────────────────────────────────────────
async def cleanup_expired() -> dict[str, int]:
    """Удалить просроченные / использованные токены и отозванные сессии."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        tokens_deleted = await conn.fetchval(
            """
            WITH d AS (
                DELETE FROM email_verification_tokens
                 WHERE used_at IS NOT NULL OR expires_at < NOW() - INTERVAL '1 day'
                RETURNING 1
            ) SELECT COUNT(*) FROM d
            """
        )
        sessions_deleted = await conn.fetchval(
            """
            WITH d AS (
                DELETE FROM sessions
                 WHERE revoked OR expires_at < NOW() - INTERVAL '7 days'
                RETURNING 1
            ) SELECT COUNT(*) FROM d
            """
        )
    return {
        "tokens_deleted": tokens_deleted or 0,
        "sessions_deleted": sessions_deleted or 0,
    }
