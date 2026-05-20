"""HTML + plain-text шаблоны писем для AImail.

Каждая функция возвращает (subject, plain_body, html_body).
Шаблоны намеренно простые и self-contained — без Jinja, чтобы избежать
дополнительной зависимости и заодно держать письма легко-аудируемыми.

Стиль писем:
  • Заголовок и кнопка — в стиле «mail-friendly» (inline CSS, без внешних
    стилей и без JS — некоторые клиенты вырежут).
  • Текст по-русски, без терминологии.
  • Plain-text дублирует HTML для тех клиентов, кто HTML не рендерит,
    и для антиспам-фильтров (Gmail хуже ранжирует HTML-only письма).
"""
from __future__ import annotations

from html import escape


# ─────────────────────────────────────────────────────────────────────────
# Общий wrapper: одинаковый header/footer для всех писем
# ─────────────────────────────────────────────────────────────────────────
def _wrap_html(title: str, inner_html: str, footer_html: str = "") -> str:
    """Оборачивает body в стандартный HTML-каркас с inline-стилями."""
    safe_title = escape(title)
    default_footer = (
        "Это письмо отправлено помощником AImail автоматически. "
        "Если вы не запрашивали — просто игнорируйте, аккаунт не создан."
    )
    footer = footer_html or default_footer
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
</head>
<body style="margin:0;padding:0;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a1a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f6f8;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.04);">
        <tr><td style="padding:28px 32px 8px 32px;">
          <div style="font-size:13px;color:#888;letter-spacing:0.4px;text-transform:uppercase;">AImail</div>
        </td></tr>
        <tr><td style="padding:8px 32px 32px 32px;font-size:16px;line-height:1.55;">
{inner_html}
        </td></tr>
        <tr><td style="padding:18px 32px 28px 32px;border-top:1px solid #eee;font-size:12px;color:#999;line-height:1.5;">
          {footer}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────
# 1a. OTP-CODE письмо (вход по 6-значному коду) — основной flow с 2026-05-20
# ─────────────────────────────────────────────────────────────────────────
def otp_code(
    *,
    email: str,
    code: str,
    ttl_minutes: int = 15,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, str]:
    """Письмо с одноразовым 6-значным кодом для входа.

    Возвращает: (subject, plain_text, html)
    """
    safe_email = escape(email)
    safe_code = escape(code)
    ttl_human = f"{ttl_minutes} минут" if ttl_minutes != 1 else "1 минуту"

    context_lines: list[str] = []
    if ip_address:
        context_lines.append(f"Адрес запроса: {escape(ip_address)}")
    if user_agent:
        ua_short = user_agent[:120] + ("…" if len(user_agent) > 120 else "")
        context_lines.append(f"Устройство: {escape(ua_short)}")
    context_block = ""
    if context_lines:
        context_block = (
            "<p style=\"font-size:13px;color:#777;margin-top:24px;\">"
            + "<br>".join(context_lines)
            + "</p>"
        )

    subject = f"Код входа в AImail: {code}"

    plain = (
        f"Здравствуйте!\n"
        f"\n"
        f"Кто-то (надеемся, что вы) запросил вход в AImail для адреса {email}.\n"
        f"Введите этот код на странице входа — он действителен {ttl_human}\n"
        f"и работает один раз.\n"
        f"\n"
        f"    {code}\n"
        f"\n"
        f"Если это были не вы — просто проигнорируйте письмо, аккаунт не создан\n"
        f"и доступа никто не получит.\n"
        f"\n"
        f"— AImail"
    )

    inner = f"""
          <h1 style="font-size:22px;margin:0 0 16px 0;line-height:1.3;color:#1a1a1a;">Код входа в AImail</h1>
          <p style="margin:0 0 18px 0;">Здравствуйте! Кто-то запросил вход для адреса <strong>{safe_email}</strong>.</p>
          <p style="margin:0 0 16px 0;">Введите этот код на странице входа:</p>
          <div style="margin:18px 0 8px 0;text-align:center;">
            <div style="display:inline-block;padding:18px 32px;background:#f3f6fb;border:2px solid #d3dce8;border-radius:14px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:34px;font-weight:700;letter-spacing:8px;color:#1a73e8;">{safe_code}</div>
          </div>
          <p style="margin:18px 0 6px 0;font-size:13px;color:#666;text-align:center;">Действителен {ttl_human}, работает один раз.</p>
          {context_block}
          <p style="margin:24px 0 0 0;font-size:13px;color:#777;">Если вы не запрашивали вход — просто игнорируйте письмо. Никаких действий не нужно.</p>
"""
    html = _wrap_html(subject, inner)
    return subject, plain, html


# ─────────────────────────────────────────────────────────────────────────
# 1b. MAGIC-LINK письмо (legacy: ссылка из письма) — оставлено для backward-compat
# ─────────────────────────────────────────────────────────────────────────
def magic_link(
    *,
    email: str,
    magic_link_url: str,
    ttl_minutes: int = 15,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, str]:
    """Письмо с одноразовой ссылкой для входа.

    Возвращает: (subject, plain_text, html)
    """
    safe_email = escape(email)
    safe_url = escape(magic_link_url, quote=True)
    ttl_human = f"{ttl_minutes} минут" if ttl_minutes != 1 else "1 минуту"

    # Дополнительная строка с контекстом запроса — повышает доверие
    context_lines: list[str] = []
    if ip_address:
        context_lines.append(f"Адрес запроса: {escape(ip_address)}")
    if user_agent:
        # Урезаем длинный user-agent чтобы не пугать
        ua_short = user_agent[:120] + ("…" if len(user_agent) > 120 else "")
        context_lines.append(f"Устройство: {escape(ua_short)}")
    context_block = ""
    if context_lines:
        context_block = (
            "<p style=\"font-size:13px;color:#777;margin-top:24px;\">"
            + "<br>".join(context_lines)
            + "</p>"
        )

    subject = "Вход в AImail — ваша ссылка"

    plain = (
        f"Здравствуйте!\n"
        f"\n"
        f"Кто-то (надеемся, что вы) запросил вход в AImail для адреса {email}.\n"
        f"Откройте ссылку ниже — это завершит вход. Ссылка действительна {ttl_human}\n"
        f"и работает один раз.\n"
        f"\n"
        f"{magic_link_url}\n"
        f"\n"
        f"Если это были не вы — просто проигнорируйте письмо, аккаунт не создан\n"
        f"и доступа никто не получит.\n"
        f"\n"
        f"— AImail"
    )

    inner = f"""
          <h1 style="font-size:22px;margin:0 0 16px 0;line-height:1.3;color:#1a1a1a;">Вход в AImail</h1>
          <p style="margin:0 0 18px 0;">Здравствуйте! Кто-то запросил вход для адреса <strong>{safe_email}</strong>.</p>
          <p style="margin:0 0 26px 0;">Нажмите кнопку — это завершит вход. Ссылка работает один раз и действительна {ttl_human}.</p>
          <p style="margin:0 0 26px 0;text-align:center;">
            <a href="{safe_url}" style="display:inline-block;padding:14px 28px;background:#1a73e8;color:#ffffff;text-decoration:none;border-radius:10px;font-weight:600;font-size:15px;">Войти в AImail</a>
          </p>
          <p style="margin:0 0 8px 0;font-size:13px;color:#666;">Если кнопка не работает — скопируйте адрес в браузер:</p>
          <p style="margin:0;font-size:12px;color:#888;word-break:break-all;background:#f7f7f9;padding:10px 12px;border-radius:8px;">{safe_url}</p>
          {context_block}
          <p style="margin:24px 0 0 0;font-size:13px;color:#777;">Если вы не запрашивали вход — просто игнорируйте письмо. Никаких действий не нужно.</p>
"""
    html = _wrap_html(subject, inner)
    return subject, plain, html


# ─────────────────────────────────────────────────────────────────────────
# 2. WELCOME письмо (после первого входа)
# ─────────────────────────────────────────────────────────────────────────
def welcome(
    *,
    email: str,
    profile_url: str,
    is_owner: bool = False,
) -> tuple[str, str, str]:
    """Приветственное письмо после первой регистрации.

    Для bootstrap-владельца (is_owner=True) текст расширенный — о доступе к
    административной части.
    """
    safe_email = escape(email)
    safe_profile = escape(profile_url, quote=True)
    subject = "Добро пожаловать в AImail" if not is_owner else "AImail — владельческий доступ активирован"

    owner_block_plain = ""
    owner_block_html = ""
    if is_owner:
        owner_block_plain = (
            "\n"
            "Это аккаунт владельца. Все существующие комнаты и помощники, ранее работавшие\n"
            "под служебным кодом, теперь привязаны к вам. Административная панель — по адресу\n"
            f"{profile_url.rsplit('/', 1)[0]}/admin\n"
        )
        owner_block_html = f"""
          <div style="margin:22px 0;padding:14px 16px;background:#fff7e6;border-left:3px solid #f5a623;border-radius:8px;font-size:14px;color:#5a4400;">
            <strong>Владельческий доступ.</strong> Все существующие комнаты и помощники привязаны к этому аккаунту. Административная панель доступна сразу.
          </div>
"""

    plain = (
        f"Здравствуйте!\n"
        f"\n"
        f"Аккаунт {email} в AImail создан и готов к работе.\n"
        f"{owner_block_plain}"
        f"\n"
        f"Чтобы посмотреть свои комнаты и помощников, откройте профиль:\n"
        f"{profile_url}\n"
        f"\n"
        f"Если есть вопросы — ответьте на это письмо.\n"
        f"\n"
        f"— AImail"
    )

    inner = f"""
          <h1 style="font-size:22px;margin:0 0 16px 0;line-height:1.3;color:#1a1a1a;">Добро пожаловать!</h1>
          <p style="margin:0 0 18px 0;">Аккаунт <strong>{safe_email}</strong> создан и готов к работе.</p>
          {owner_block_html}
          <p style="margin:0 0 22px 0;">В профиле собраны ваши комнаты, помощники и настройки уведомлений.</p>
          <p style="margin:0 0 26px 0;text-align:center;">
            <a href="{safe_profile}" style="display:inline-block;padding:12px 24px;background:#1a73e8;color:#ffffff;text-decoration:none;border-radius:10px;font-weight:600;font-size:15px;">Открыть профиль</a>
          </p>
          <p style="margin:0;font-size:13px;color:#777;">Если что-то непонятно — просто ответьте на это письмо, мы прочитаем.</p>
"""
    html = _wrap_html(subject, inner)
    return subject, plain, html


# ─────────────────────────────────────────────────────────────────────────
# 3. NOTIFICATION письмо (универсальное, для будущих уведомлений)
# ─────────────────────────────────────────────────────────────────────────
def notification(
    *,
    email: str,
    title: str,
    body_text: str,
    action_url: str | None = None,
    action_label: str | None = None,
) -> tuple[str, str, str]:
    """Универсальное письмо-уведомление (новый ответ в комнате, заверш. задача, …).

    body_text — обычный текст, переносы строк сохраняются.
    """
    _ = email  # email пока не используется в теле, но оставлен для логирования
    safe_title = escape(title)
    safe_body_html = escape(body_text).replace("\n", "<br>")

    subject = title

    action_block_plain = ""
    action_block_html = ""
    if action_url and action_label:
        safe_url = escape(action_url, quote=True)
        safe_label = escape(action_label)
        action_block_plain = f"\n{action_label}: {action_url}\n"
        action_block_html = f"""
          <p style="margin:22px 0 0 0;text-align:center;">
            <a href="{safe_url}" style="display:inline-block;padding:12px 24px;background:#1a73e8;color:#ffffff;text-decoration:none;border-radius:10px;font-weight:600;font-size:14px;">{safe_label}</a>
          </p>
"""

    plain = (
        f"{title}\n"
        f"\n"
        f"{body_text}\n"
        f"{action_block_plain}"
        f"\n"
        f"— AImail"
    )

    inner = f"""
          <h1 style="font-size:20px;margin:0 0 14px 0;line-height:1.3;color:#1a1a1a;">{safe_title}</h1>
          <div style="margin:0;font-size:15px;line-height:1.6;color:#333;">{safe_body_html}</div>
          {action_block_html}
"""
    html = _wrap_html(subject, inner)
    return subject, plain, html
