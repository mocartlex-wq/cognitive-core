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
# Email-safe «Liquid Glass» каркас OTP-письма (таблицы + inline CSS, без JS,
# без backdrop-filter как load-bearing). Плейсхолдеры подставляются в otp_code().
# НЕ f-string — поэтому CSS-фигурные скобки целы.
_OTP_HTML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html lang="ru" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="X-UA-Compatible" content="IE=edge" />
  <meta name="color-scheme" content="light" />
  <meta name="supported-color-schemes" content="light" />
  <title>Код входа в AImail</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    body { margin:0; padding:0; width:100% !important; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
    table { border-collapse:collapse; mso-table-lspace:0pt; mso-table-rspace:0pt; }
    a { text-decoration:none; }
    @supports ((-webkit-backdrop-filter:blur(1px)) or (backdrop-filter:blur(1px))) {
      .glass { -webkit-backdrop-filter:blur(20px); backdrop-filter:blur(20px); }
    }
    .codegrad {
      background:linear-gradient(135deg,#6366f1,#db2777);
      -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
    }
    @media only screen and (max-width:620px) {
      .container { width:100% !important; }
      .px { padding-left:22px !important; padding-right:22px !important; }
      .code { font-size:34px !important; letter-spacing:8px !important; }
    }
  </style>
</head>
<body style="margin:0; padding:0; background:#e8ecff;">

  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all; opacity:0; color:transparent; height:0; width:0; font-size:1px; line-height:1px;">
    Ваш код входа в AImail: __CODE__. Действует __TTL__, один раз.&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;&#8204;&nbsp;
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#e8ecff"
         style="background:#e8ecff; background:radial-gradient(120% 120% at 0% 0%,#e0e7ff 0%,#fbe8f3 45%,#e6f0ff 100%);">
    <tr>
      <td align="center" style="padding:36px 16px;">

        <!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
        <table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px; max-width:600px; margin:0 auto;">

          <tr>
            <td class="glass px" bgcolor="#faf9ff"
                style="background:rgba(255,255,255,0.62); border:1px solid rgba(255,255,255,0.75); border-radius:22px; padding:36px 32px;
                       box-shadow:0 24px 60px rgba(80,70,160,0.18); text-align:center;
                       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif; color:#26203a;">

              <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto 18px;">
                <tr>
                  <td width="48" height="48" align="center" valign="middle" bgcolor="#7c5cf0"
                      style="width:48px; height:48px; background:linear-gradient(135deg,#6366f1,#a855f7); border-radius:13px;
                             color:#ffffff; font-size:22px; line-height:48px; mso-line-height-rule:exactly;">&#9993;</td>
                </tr>
              </table>

              <h1 style="margin:0 0 8px; font-size:21px; font-weight:700; color:#26203a;">Код входа в AImail</h1>
              <p style="margin:0 0 24px; font-size:14px; line-height:1.5; color:#6b6582;">
                Кто-то запросил вход для<br /><b style="color:#4a4360;">__EMAIL__</b>
              </p>

              <div class="code codegrad" style="font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,Courier,monospace;
                          font-size:40px; font-weight:800; letter-spacing:12px; color:#6d28d9; padding:6px 0 2px;">__CODE__</div>
              <p style="margin:6px 0 24px; font-size:12px; color:#8b85a0;">Действует __TTL__&nbsp;·&nbsp;один раз</p>

              <!--[if mso]>
              <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word"
                           href="__LOGIN_URL__"
                           style="height:50px;v-text-anchor:middle;width:330px;" arcsize="28%" stroke="f" fillcolor="#7c5cf0">
                <w:anchorlock/>
                <center style="color:#ffffff;font-family:Arial,sans-serif;font-size:15px;font-weight:bold;">Войти с этим кодом &#8594;</center>
              </v:roundrect>
              <![endif]-->
              <!--[if !mso]><!-- -->
              <a href="__LOGIN_URL__"
                 style="display:inline-block; background:#7c5cf0; background:linear-gradient(135deg,#6366f1,#a855f7);
                        color:#ffffff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
                        font-size:15px; font-weight:700; line-height:20px; text-decoration:none; border-radius:14px;
                        padding:15px 34px; box-shadow:0 12px 26px rgba(124,58,237,0.35);">Войти с этим кодом &#8594;</a>
              <!--<![endif]-->

              <p style="margin:16px 0 0; font-size:12.5px; line-height:1.55; color:#7a7392;">
                Кнопка не сработала? Введите код вручную на странице входа<br />
                <a href="__LOGIN_URL__" style="color:#4f46e5; text-decoration:underline;">__LOGIN_HOST__</a>
              </p>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:24px;">
                <tr><td style="border-top:1px solid rgba(124,58,237,0.14); padding-top:18px; font-size:11.5px; line-height:1.6; color:#9089a3;">
                  __CONTEXT__
                </td></tr>
              </table>

            </td>
          </tr>

          <tr>
            <td align="center" style="padding:20px 12px 4px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif; font-size:11px; line-height:1.5; color:#9b95ad;">
              AImail&nbsp;·&nbsp;me-ai.ru<br />Письмо отправлено автоматически — отвечать на него не нужно.
            </td>
          </tr>

        </table>
        <!--[if mso]></td></tr></table><![endif]-->

      </td>
    </tr>
  </table>

</body>
</html>"""


def otp_code(
    *,
    email: str,
    code: str,
    ttl_minutes: int = 15,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, str]:
    """Письмо с одноразовым 6-значным кодом для входа (дизайн «Liquid Glass»).

    Возвращает: (subject, plain_text, html). Email-safe (таблицы + inline CSS,
    без JS). Крупный код для ручного ввода + кнопка-ссылка на /ui/login с
    префиллом (?e=&code=) — автозаполнение на странице входа.
    """
    from urllib.parse import quote

    try:
        from app.config import settings

        login_base = (settings.app_url or "https://aimail.art").rstrip("/")
    except Exception:
        login_base = "https://aimail.art"

    safe_email = escape(email)
    safe_code = escape(code)
    ttl_human = f"{ttl_minutes} минут" if ttl_minutes != 1 else "1 минуту"

    login_url = f"{login_base}/ui/login?e={quote(email)}&code={quote(code)}"
    safe_login_url = escape(login_url, quote=True)
    login_host = login_base.split("//", 1)[-1].rstrip("/") + "/ui/login"

    # Контекст запроса (IP / устройство) — повышает доверие
    ctx_parts: list[str] = []
    if ip_address:
        ctx_parts.append(f"Запрос с {escape(ip_address)}")
    if user_agent:
        ua_short = user_agent[:80] + ("…" if len(user_agent) > 80 else "")
        ctx_parts.append(escape(ua_short))
    ctx_prefix = ("&nbsp;·&nbsp;".join(ctx_parts) + ".<br />") if ctx_parts else ""
    context_html = ctx_prefix + "Это не вы? Просто игнорируйте письмо — аккаунт не создан."

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
        f"Или откройте ссылку (код подставится сам):\n{login_url}\n"
        f"\n"
        f"Если это были не вы — просто проигнорируйте письмо, аккаунт не создан\n"
        f"и доступа никто не получит.\n"
        f"\n"
        f"— AImail"
    )

    html = (
        _OTP_HTML.replace("__CODE__", safe_code)
        .replace("__EMAIL__", safe_email)
        .replace("__TTL__", ttl_human)
        .replace("__LOGIN_URL__", safe_login_url)
        .replace("__LOGIN_HOST__", escape(login_host))
        .replace("__CONTEXT__", context_html)
    )
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
        owner_block_html = """
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
