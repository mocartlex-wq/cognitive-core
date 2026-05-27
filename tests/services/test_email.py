"""Unit tests для app/services/email_client.py + email_templates.py.

Pure-logic + mocked aiosmtplib. Без реального SMTP-сервера.

Modules under test:
  • app.services.email_templates — pure functions (subject, plain, html)
  • app.services.email_client    — send_email() с 3 бекендами: yandex/postfix/stdout

Approach:
  • Templates тестируем напрямую — никаких моков, проверяем содержимое
    + HTML-escape (XSS-safe) + сохранение Cyrillic.
  • EmailClient тестируем с aiosmtplib.send замоканым через AsyncMock,
    проверяем что переданы правильные host/port/auth и MIME-структура корректна.
  • settings monkeypatch — для переключения backend между tests.

TODO: API surface взят из локального клона репо на 2026-05-27;
если в проде сигнатуры функций отличаются — тесты упадут с понятной ошибкой.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services import email_client, email_templates


# ─────────────────────────────────────────────────────────────────────────
# TestEmailTemplates — pure functions, no IO, no mocks
# ─────────────────────────────────────────────────────────────────────────
class TestEmailTemplates:
    def test_otp_code_contains_code_and_email(self):
        subject, plain, html = email_templates.otp_code(
            email="user@example.com",
            code="123456",
        )
        assert "123456" in subject
        assert "123456" in plain
        assert "123456" in html
        assert "user@example.com" in plain
        assert "user@example.com" in html

    def test_magic_link_contains_url(self):
        url = "https://aimail.art/auth/verify?token=abc123XYZ"
        subject, plain, html = email_templates.magic_link(
            email="user@example.com",
            magic_link_url=url,
        )
        assert url in plain
        # html escapes "&" если есть в URL — тут чистый URL, должен быть as-is
        assert url in html
        assert "user@example.com" in plain
        assert "AImail" in subject

    def test_welcome_contains_email_and_profile(self):
        subject, plain, html = email_templates.welcome(
            email="newbie@example.com",
            profile_url="https://aimail.art/ui/profile",
        )
        # NOTE: welcome() не принимает display_name — использует email
        # как основной идентификатор (см. email_templates.py:193).
        assert "newbie@example.com" in plain
        assert "newbie@example.com" in html
        assert "https://aimail.art/ui/profile" in plain
        assert "Добро пожаловать" in subject

    def test_welcome_owner_variant(self):
        subject, _plain, html = email_templates.welcome(
            email="owner@example.com",
            profile_url="https://aimail.art/ui/profile",
            is_owner=True,
        )
        assert "владельческий" in subject.lower() or "владелец" in subject.lower()
        # owner-блок должен быть в html (про админ-панель)
        assert "Владельческий" in html or "админ" in html.lower()

    def test_html_xss_safe_in_email(self):
        """Email с HTML-символами должен быть escaped в HTML-output."""
        xss = "<script>alert(1)</script>@evil.tld"
        _subject, _plain, html = email_templates.otp_code(
            email=xss,
            code="000000",
        )
        # raw <script> НЕ должен присутствовать
        assert "<script>" not in html
        # должна быть escape-форма
        assert "&lt;script&gt;" in html

    def test_html_xss_safe_in_otp_code(self):
        """Code тоже escapes (на случай если когда-нибудь будет non-numeric)."""
        _subject, _plain, html = email_templates.otp_code(
            email="x@y.z",
            code="<b>hax</b>",
        )
        assert "<b>hax</b>" not in html
        assert "&lt;b&gt;hax&lt;/b&gt;" in html

    def test_magic_link_url_xss_safe(self):
        """URL с javascript: и quotes должен быть escaped."""
        evil_url = 'javascript:alert("xss")'
        _subject, _plain, html = email_templates.magic_link(
            email="x@y.z",
            magic_link_url=evil_url,
        )
        # quote=True должен escape двойные кавычки в href
        assert '"xss"' not in html  # raw quotes не должны "сломать" атрибут
        assert "&quot;" in html

    def test_cyrillic_preserved_in_email(self):
        """Cyrillic в email-локали (IDN-style) — НЕ ломается, UTF-8 preserved."""
        _subject, plain, html = email_templates.otp_code(
            email="пользователь@example.com",
            code="654321",
        )
        assert "пользователь@example.com" in plain
        assert "пользователь@example.com" in html

    def test_cyrillic_in_notification_body(self):
        """Notification body с кириллицей — preserved + newlines → <br>."""
        body = "Привет!\nЭто тест с переносом строки.\nИ ещё одна строка."
        subject, plain, html = email_templates.notification(
            email="user@example.com",
            title="Тестовое уведомление",
            body_text=body,
        )
        assert "Тестовое уведомление" == subject
        assert "Привет!" in plain
        assert "Привет!" in html
        # newlines в html заменены на <br>
        assert "<br>" in html

    def test_notification_with_action_button(self):
        subject, plain, html = email_templates.notification(
            email="x@y.z",
            title="Новое сообщение",
            body_text="Открой ссылку.",
            action_url="https://aimail.art/room/42",
            action_label="Перейти в комнату",
        )
        assert "https://aimail.art/room/42" in plain
        assert "https://aimail.art/room/42" in html
        assert "Перейти в комнату" in html
        assert subject == "Новое сообщение"


# ─────────────────────────────────────────────────────────────────────────
# TestEmailClient — send_email() с замоканым aiosmtplib
# ─────────────────────────────────────────────────────────────────────────
class TestEmailClient:
    @pytest.mark.asyncio
    async def test_send_email_yandex_backend(self, monkeypatch):
        """Yandex backend: host/port/auth должны быть прокинуты в aiosmtplib.send."""
        monkeypatch.setattr(settings, "email_backend", "yandex")
        monkeypatch.setattr(settings, "smtp_host", "smtp.yandex.ru")
        monkeypatch.setattr(settings, "smtp_port", 465)
        monkeypatch.setattr(settings, "smtp_user", "mozartlex@yandex.ru")
        monkeypatch.setattr(settings, "smtp_password", "app-password-xyz")
        monkeypatch.setattr(settings, "email_from", "mozartlex@yandex.ru")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        mock_send = AsyncMock()
        with patch("aiosmtplib.send", mock_send):
            result = await email_client.send_email(
                to="dest@example.com",
                subject="Test",
                plain_text="hello",
                html_text="<p>hello</p>",
            )

        assert result.success is True
        assert result.backend == "yandex"
        assert result.message_id.startswith("<") and result.message_id.endswith(">")
        mock_send.assert_awaited_once()
        kwargs = mock_send.await_args.kwargs
        assert kwargs["hostname"] == "smtp.yandex.ru"
        assert kwargs["port"] == 465
        assert kwargs["username"] == "mozartlex@yandex.ru"
        assert kwargs["password"] == "app-password-xyz"
        assert kwargs["use_tls"] is True
        assert kwargs["start_tls"] is False
        assert kwargs["recipients"] == ["dest@example.com"]

    @pytest.mark.asyncio
    async def test_send_email_postfix_backend(self, monkeypatch):
        """Postfix backend: STARTTLS на 587, auth optional."""
        monkeypatch.setattr(settings, "email_backend", "postfix")
        monkeypatch.setattr(settings, "smtp_host", "mail.aimail.art")
        monkeypatch.setattr(settings, "smtp_port", 587)
        monkeypatch.setattr(settings, "smtp_user", None)
        monkeypatch.setattr(settings, "smtp_password", None)
        monkeypatch.setattr(settings, "email_from", "noreply@aimail.art")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        mock_send = AsyncMock()
        with patch("aiosmtplib.send", mock_send):
            result = await email_client.send_email(
                to="dest@example.com",
                subject="Hi",
                plain_text="hi",
                html_text="<p>hi</p>",
            )

        assert result.success is True
        assert result.backend == "postfix"
        kwargs = mock_send.await_args.kwargs
        assert kwargs["hostname"] == "mail.aimail.art"
        assert kwargs["port"] == 587
        assert kwargs["use_tls"] is False
        assert kwargs["start_tls"] is True
        # auth не передан — нет username/password в kwargs
        assert "username" not in kwargs
        assert "password" not in kwargs

    @pytest.mark.asyncio
    async def test_send_email_stdout_backend(self, monkeypatch, caplog):
        """Stdout backend: не зовёт aiosmtplib, пишет в logger."""
        monkeypatch.setattr(settings, "email_backend", "stdout")
        monkeypatch.setattr(settings, "email_from", "noreply@aimail.art")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        mock_send = AsyncMock()
        with patch("aiosmtplib.send", mock_send), caplog.at_level("INFO"):
            result = await email_client.send_email(
                to="dest@example.com",
                subject="Console subj",
                plain_text="body",
                html_text="<p>body</p>",
            )

        assert result.success is True
        assert result.backend == "stdout"
        # aiosmtplib НЕ должен быть вызван
        mock_send.assert_not_awaited()
        # В лог должна попасть info про email
        assert any("EMAIL[stdout]" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_send_email_smtp_failure_returns_error(self, monkeypatch):
        """SMTP error → SendResult.success=False + .error filled. НЕ raise."""
        monkeypatch.setattr(settings, "email_backend", "yandex")
        monkeypatch.setattr(settings, "smtp_host", "smtp.yandex.ru")
        monkeypatch.setattr(settings, "smtp_port", 465)
        monkeypatch.setattr(settings, "smtp_user", "u@y.ru")
        monkeypatch.setattr(settings, "smtp_password", "pw")
        monkeypatch.setattr(settings, "email_from", "u@y.ru")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        # OSError ловится в основном except — независимо от наличия aiosmtplib
        mock_send = AsyncMock(side_effect=OSError("connection refused"))
        with patch("aiosmtplib.send", mock_send):
            result = await email_client.send_email(
                to="dest@example.com",
                subject="Test",
                plain_text="x",
                html_text="<p>x</p>",
            )

        assert result.success is False
        assert result.backend == "yandex"
        assert result.error is not None
        assert "OSError" in result.error
        assert "connection refused" in result.error

    @pytest.mark.asyncio
    async def test_send_email_unknown_backend_returns_error(self, monkeypatch):
        """Unknown backend → SendResult.success=False (не raise)."""
        monkeypatch.setattr(settings, "email_backend", "sendgrid_lol")
        monkeypatch.setattr(settings, "email_from", "noreply@aimail.art")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        result = await email_client.send_email(
            to="dest@example.com",
            subject="Test",
            plain_text="x",
            html_text="<p>x</p>",
        )
        assert result.success is False
        assert result.error is not None
        assert "sendgrid_lol" in result.error.lower() or "unexpected" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_email_cyrillic_subject_and_body(self, monkeypatch):
        """Cyrillic в subject/body — корректно encoded UTF-8 в MIME."""
        monkeypatch.setattr(settings, "email_backend", "yandex")
        monkeypatch.setattr(settings, "smtp_host", "smtp.yandex.ru")
        monkeypatch.setattr(settings, "smtp_port", 465)
        monkeypatch.setattr(settings, "smtp_user", "u@y.ru")
        monkeypatch.setattr(settings, "smtp_password", "pw")
        monkeypatch.setattr(settings, "email_from", "u@y.ru")
        monkeypatch.setattr(settings, "email_from_name", "AImail")
        monkeypatch.setattr(settings, "email_reply_to", None)

        mock_send = AsyncMock()
        with patch("aiosmtplib.send", mock_send):
            result = await email_client.send_email(
                to="dest@example.com",
                subject="Код входа в AImail: 123456",
                plain_text="Здравствуйте! Ваш код: 123456",
                html_text="<p>Здравствуйте! Ваш код: <strong>123456</strong></p>",
            )

        assert result.success is True
        # Reach into the MIMEMultipart that was passed as first positional arg
        msg = mock_send.await_args.args[0]
        # Subject должен быть encoded (либо raw UTF-8, либо =?utf-8?b?...?=)
        subject_header = str(msg["Subject"])
        assert "Код входа" in subject_header or "utf-8" in subject_header.lower()
        # Body parts должны быть UTF-8
        body_str = msg.as_string()
        # либо raw cyrillic, либо base64-encoded (charset=utf-8)
        assert "utf-8" in body_str.lower()
