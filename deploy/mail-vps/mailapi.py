"""mailapi — мини HTTP API для отправки писем через локальный Postfix.

Слушает на 127.0.0.1:8001 (внутри VPS). Принимает POST /send с JSON:
    {
      "to": "user@example.com",
      "subject": "Тема",
      "html": "<html>...</html>",
      "text": "plain text",
      "from": "noreply@aimail.art"        # опц., дефолт settings
    }
и кладёт письмо в очередь Postfix через локальный `sendmail`.

Аутентификация: shared API key через заголовок `X-API-Key`. Ключ задан
в `/etc/cogcore-mailapi.env` как `MAILAPI_KEY=<random>`. cognitive-core
держит этот же ключ в `.env` (SMTP_PASSWORD при backend=postfix не нужен,
если используется этот HTTP-канал вместо прямого SMTP).

Зачем отдельный HTTP-канал, а не прямой SMTP:
  + Не нужен SASL — простой shared-key
  + Audit-лог каждого письма (mailapi.log)
  + Rate-limit на уровне приложения
  + Не торчит naружу — открыт только nginx-proxy для cognitive-core

В простом варианте (для старта) cognitive-core может ходить напрямую
на 587 порт mail-VPS через SMTP — тогда этот mailapi не нужен.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/var/log/cogcore-mailapi.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("mailapi")

API_KEY = os.environ.get("MAILAPI_KEY", "")
DEFAULT_FROM = os.environ.get("MAILAPI_FROM", "noreply@aimail.art")
DEFAULT_FROM_NAME = os.environ.get("MAILAPI_FROM_NAME", "AImail")
RATE_LIMIT_PER_HOUR = int(os.environ.get("MAILAPI_RATE_LIMIT_PER_HOUR", "1000"))

if not API_KEY:
    raise RuntimeError("MAILAPI_KEY not set in env (см. /etc/cogcore-mailapi.env)")

app = FastAPI(title="cogcore-mailapi", version="1.0")

# In-memory rate-limiter (на старте достаточно; для много-инстанс — Redis)
_rate_window: list[float] = []


def _check_rate_limit() -> None:
    now = time.time()
    # держим только записи за последний час
    cutoff = now - 3600
    while _rate_window and _rate_window[0] < cutoff:
        _rate_window.pop(0)
    if len(_rate_window) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Rate-limit {RATE_LIMIT_PER_HOUR}/час превышен",
        )
    _rate_window.append(now)


class SendBody(BaseModel):
    to: EmailStr
    subject: str = Field(..., max_length=200)
    html: str = Field(..., max_length=200_000)
    text: str = Field(..., max_length=200_000)
    from_: str | None = Field(None, alias="from", max_length=200)


@app.post("/send")
async def send_email(body: SendBody, request: Request):
    # Auth
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")

    _check_rate_limit()

    msg = EmailMessage()
    msg["From"] = body.from_ or f"{DEFAULT_FROM_NAME} <{DEFAULT_FROM}>"
    msg["To"] = body.to
    msg["Subject"] = body.subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{DEFAULT_FROM.rsplit('@', 1)[-1]}>"
    msg["X-Mailer"] = "cogcore-mailapi/1.0"
    msg.set_content(body.text, charset="utf-8")
    msg.add_alternative(body.html, subtype="html", charset="utf-8")

    # Отправляем через локальный sendmail (Postfix)
    try:
        proc = subprocess.run(
            ["/usr/sbin/sendmail", "-t", "-oi"],
            input=msg.as_bytes(),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="ignore")[:500]
            logger.error("sendmail failed rc=%d stderr=%s", proc.returncode, stderr)
            raise HTTPException(status_code=502, detail=f"sendmail rc={proc.returncode}")
    except subprocess.TimeoutExpired:
        logger.error("sendmail timeout to=%s", body.to)
        raise HTTPException(status_code=504, detail="sendmail timeout")

    logger.info("sent to=%s subject=%r mid=%s", body.to, body.subject, msg["Message-ID"])
    return {"ok": True, "message_id": msg["Message-ID"]}


@app.get("/health")
async def health():
    # Проверяем что sendmail доступен
    sendmail_ok = os.path.exists("/usr/sbin/sendmail")
    return {
        "ok": True,
        "sendmail": sendmail_ok,
        "rate_window_size": len(_rate_window),
        "rate_limit_per_hour": RATE_LIMIT_PER_HOUR,
    }
