"""Async SMTP-клиент с двумя бекендами: Yandex (на старте) и Postfix (после прогрева).

Архитектура:
  ┌──────────────────────────────────────────────────────────────────┐
  │  app/api/auth.py → send_email(to, subject, plain, html, kind)    │
  └──────────────┬───────────────────────────────────────────────────┘
                 │
                 ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  EmailClient — выбирает backend по settings.email_backend:       │
  │     • "yandex"  → smtp.yandex.ru:465 (SSL), auth обязательна,    │
  │                   From: mozartlex@yandex.ru, Reply-To: aimail.art│
  │     • "postfix" → mail.aimail.art:587 (STARTTLS, наш VPS),       │
  │                   From: noreply@aimail.art                       │
  │     • "stdout"  → пишет письмо в stdout (для dev/test)           │
  └──────────────────────────────────────────────────────────────────┘

Переключение бекенда — только через .env (EMAIL_BACKEND=postfix), без code change.

Антиспам-best-practices:
  • Message-ID с доменом отправителя
  • Date в RFC2822 формате
  • multipart/alternative: plain + html
  • In-Reply-To / References — пустые (письма не treads)
  • List-Unsubscribe header для magic-link не делаем (это транзакционные,
    не маркетинг) — но добавим для notification если потребуется

Retry: одна попытка немедленно, потом outbox-event в очередь cron-retry.
Это не критично для magic-link (пользователь сам пере-запросит),
но важно для notification.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import socket
import uuid
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import aiosmtplib  # type: ignore
except ImportError:  # pragma: no cover — на dev-машине без зависимости
    aiosmtplib = None  # noqa: N816 — лениво проверим в runtime

from app.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Public dataclass: что вернул send_email()
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class SendResult:
    success: bool
    backend: str
    message_id: str
    error: str | None = None
    duration_ms: int = 0


# ─────────────────────────────────────────────────────────────────────────
# Внутреннее: построение MIME-сообщения
# ─────────────────────────────────────────────────────────────────────────
def _build_message(
    *,
    to: str,
    subject: str,
    plain_text: str,
    html_text: str,
    from_email: str,
    from_name: str,
    reply_to: str | None,
) -> tuple[MIMEMultipart, str]:
    """Собирает MIME multipart/alternative.

    Возвращает (msg, message_id) — message_id для логирования + retry-dedup.
    """
    msg = MIMEMultipart("alternative")

    # Message-ID: <uuid@domain> где domain взят из from_email
    domain = from_email.rsplit("@", 1)[-1] if "@" in from_email else "aimail.art"
    message_id = f"<{uuid.uuid4().hex}@{domain}>"

    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["From"] = email.utils.formataddr((from_name, from_email))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    # Дополнительные заголовки для лучшей доставляемости
    msg["X-Mailer"] = "AImail/1.0"
    msg["MIME-Version"] = "1.0"

    # Порядок частей в multipart/alternative важен:
    # сначала plain, потом html — почтовик покажет html, но при просмотре
    # «как plain» получит plain. Если перепутать — Gmail тоже работает,
    # но Outlook 2010 может показать plain «вторым» в превью.
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    return msg, message_id


# ─────────────────────────────────────────────────────────────────────────
# Backend 1: Yandex SMTP (smtp.yandex.ru:465 SSL, auth required)
# ─────────────────────────────────────────────────────────────────────────
async def _send_yandex(msg: MIMEMultipart, recipient: str) -> None:
    """Отправка через Yandex SMTP-relay.

    • Хост:  smtp.yandex.ru
    • Порт:  465 (implicit TLS / SSL)
    • Auth:  логин = email, пароль = app-password (НЕ обычный пароль)
    • From:  должен совпадать с авторизованным email (Yandex проверяет)
    """
    if aiosmtplib is None:
        raise RuntimeError(
            "aiosmtplib не установлен. Добавьте в requirements.txt: aiosmtplib==3.*"
        )

    if not settings.smtp_user or not settings.smtp_password:
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD не настроены в .env (нужен Yandex app-password)"
        )

    await aiosmtplib.send(
        msg,
        recipients=[recipient],
        hostname=settings.smtp_host or "smtp.yandex.ru",
        port=settings.smtp_port or 465,
        username=settings.smtp_user,
        password=settings.smtp_password,
        use_tls=True,           # implicit TLS на 465
        start_tls=False,        # НЕ STARTTLS
        timeout=20.0,
    )


# ─────────────────────────────────────────────────────────────────────────
# Backend 2: свой Postfix на mail-VPS (port 587 submission, STARTTLS, auth)
# ─────────────────────────────────────────────────────────────────────────
async def _send_postfix(msg: MIMEMultipart, recipient: str) -> None:
    """Отправка через собственный Postfix-relay.

    • Хост:  mail.aimail.art (или другой mail-VPS, задаётся в .env)
    • Порт:  587 (submission, STARTTLS)
    • Auth:  через sasldb или dovecot — настраивается на mail-VPS
    """
    if aiosmtplib is None:
        raise RuntimeError(
            "aiosmtplib не установлен. Добавьте в requirements.txt: aiosmtplib==3.*"
        )

    kwargs = {
        "hostname": settings.smtp_host or "mail.aimail.art",
        "port": settings.smtp_port or 587,
        "use_tls": False,
        "start_tls": True,
        "timeout": 20.0,
    }
    if settings.smtp_user and settings.smtp_password:
        kwargs["username"] = settings.smtp_user
        kwargs["password"] = settings.smtp_password

    await aiosmtplib.send(msg, recipients=[recipient], **kwargs)


# ─────────────────────────────────────────────────────────────────────────
# Backend 3: stdout (для разработки / unit-тестов, без реальной отправки)
# ─────────────────────────────────────────────────────────────────────────
async def _send_stdout(msg: MIMEMultipart, recipient: str) -> None:
    """Не отправляет, просто печатает письмо в stdout. Используется когда
    EMAIL_BACKEND=stdout — для локальной разработки без настроенного SMTP."""
    logger.info(
        "EMAIL[stdout] to=%s subject=%s message_id=%s",
        recipient,
        msg["Subject"],
        msg["Message-ID"],
    )
    # Урезаем для лога — полное письмо может быть длинным
    body_preview = msg.as_string()[:1500]
    logger.info("EMAIL[stdout] body preview:\n%s", body_preview)


# ─────────────────────────────────────────────────────────────────────────
# Публичная функция отправки
# ─────────────────────────────────────────────────────────────────────────
async def send_email(
    *,
    to: str,
    subject: str,
    plain_text: str,
    html_text: str,
    kind: str = "transactional",  # для тегирования в логах: magic_link / welcome / notification
) -> SendResult:
    """Отправить письмо через текущий настроенный backend.

    Параметры берутся из app.config.settings:
      • email_backend       — "yandex" | "postfix" | "stdout"
      • smtp_host / port / user / password — учётные данные SMTP
      • email_from / email_from_name — отправитель
      • email_reply_to      — куда отвечают пользователи

    Возвращает SendResult — не бросает исключения наружу, ошибка приходит
    в .error и .success=False. Это нужно чтобы вызывающий код мог красиво
    показать пользователю «не получилось, попробуйте ещё раз» без 500-ой
    в API.
    """
    backend = (settings.email_backend or "yandex").lower()
    started = asyncio.get_event_loop().time()

    msg, message_id = _build_message(
        to=to,
        subject=subject,
        plain_text=plain_text,
        html_text=html_text,
        from_email=settings.email_from or "noreply@aimail.art",
        from_name=settings.email_from_name or "AImail",
        reply_to=settings.email_reply_to or None,
    )

    try:
        if backend == "yandex":
            await _send_yandex(msg, to)
        elif backend == "postfix":
            await _send_postfix(msg, to)
        elif backend == "stdout":
            await _send_stdout(msg, to)
        else:
            raise RuntimeError(f"Неизвестный EMAIL_BACKEND: {backend!r}")

        duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        logger.info(
            "email_sent backend=%s kind=%s to=%s mid=%s duration_ms=%d",
            backend, kind, to, message_id, duration_ms,
        )
        return SendResult(
            success=True,
            backend=backend,
            message_id=message_id,
            duration_ms=duration_ms,
        )

    except (aiosmtplib.SMTPException if aiosmtplib else Exception, OSError, socket.gaierror) as e:  # type: ignore[arg-type]
        duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        err = f"{type(e).__name__}: {e}"
        logger.warning(
            "email_send_failed backend=%s kind=%s to=%s mid=%s err=%s",
            backend, kind, to, message_id, err,
        )
        return SendResult(
            success=False,
            backend=backend,
            message_id=message_id,
            error=err,
            duration_ms=duration_ms,
        )
    except Exception as e:  # pragma: no cover — на всякий случай
        duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        err = f"unexpected: {type(e).__name__}: {e}"
        logger.exception(
            "email_send_unexpected backend=%s kind=%s to=%s mid=%s",
            backend, kind, to, message_id,
        )
        return SendResult(
            success=False,
            backend=backend,
            message_id=message_id,
            error=err,
            duration_ms=duration_ms,
        )


# ─────────────────────────────────────────────────────────────────────────
# Удобные обёртки поверх send_email() для конкретных типов писем
# ─────────────────────────────────────────────────────────────────────────
async def send_magic_link(
    *,
    email: str,
    magic_link_url: str,
    ttl_minutes: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SendResult:
    """Отправить magic-link письмо для входа."""
    from app.services import email_templates

    ttl = ttl_minutes or settings.magic_link_ttl_minutes or 15
    subject, plain, html = email_templates.magic_link(
        email=email,
        magic_link_url=magic_link_url,
        ttl_minutes=ttl,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return await send_email(
        to=email,
        subject=subject,
        plain_text=plain,
        html_text=html,
        kind="magic_link",
    )


async def send_welcome(
    *,
    email: str,
    profile_url: str | None = None,
    is_owner: bool = False,
) -> SendResult:
    """Приветственное письмо после первой регистрации."""
    from app.services import email_templates

    url = profile_url or f"{(settings.app_url or 'https://aimail.art').rstrip('/')}/ui/profile"
    subject, plain, html = email_templates.welcome(
        email=email,
        profile_url=url,
        is_owner=is_owner,
    )
    return await send_email(
        to=email,
        subject=subject,
        plain_text=plain,
        html_text=html,
        kind="welcome",
    )


async def send_notification(
    *,
    email: str,
    title: str,
    body_text: str,
    action_url: str | None = None,
    action_label: str | None = None,
) -> SendResult:
    """Универсальное письмо-уведомление."""
    from app.services import email_templates

    subject, plain, html = email_templates.notification(
        email=email,
        title=title,
        body_text=body_text,
        action_url=action_url,
        action_label=action_label,
    )
    return await send_email(
        to=email,
        subject=subject,
        plain_text=plain,
        html_text=html,
        kind="notification",
    )
