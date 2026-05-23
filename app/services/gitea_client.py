"""Gitea Admin API wrapper (Phase 5C).

Используется для auto-create tenant org при email-verify (welcome flow).

Если Gitea недоступен (например не задеплоен ещё или down) — функции
возвращают best-effort error и НЕ блокируют основной flow (welcome
страница покажет «Gitea пока недоступен, попробуйте позже»).

Конфигурация через env:
  GITEA_URL — base URL (default http://cognitive_gitea:3000 в docker network)
  GITEA_ADMIN_TOKEN — long-lived admin API token. Создаётся вручную в Gitea
                     admin UI → Settings → Applications → Generate New Token
                     с scopes [admin:write_user, admin:write_org]. Хранится
                     в /etc/cognitive-deploy.env как env-var.
"""
from __future__ import annotations

import logging
import os
import re
import secrets

import httpx

logger = logging.getLogger(__name__)

GITEA_URL = os.getenv("GITEA_URL", "http://cognitive_gitea:3000")
GITEA_ADMIN_TOKEN = os.getenv("GITEA_ADMIN_TOKEN", "")


def _email_to_slug(email: str) -> str:
    """Превращает email в Gitea-совместимый username/org slug.

    Правила: lowercase, alphanumeric + dash + underscore, max 40 chars,
    не должен начинаться с цифры или дефиса.
    """
    local = email.split("@", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9_-]", "-", local)[:40]
    slug = slug.lstrip("-0123456789") or "user"
    return slug or "user"


async def is_gitea_alive() -> bool:
    """Quick check — Gitea запущен и отвечает?"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as cli:
            r = await cli.get(f"{GITEA_URL}/api/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def create_user(email: str, full_name: str | None = None) -> dict:
    """Создаёт нового user в Gitea + автоматически org с тем же slug.

    Возвращает: {ok: bool, username: str, temp_password: str | None,
                 org: str | None, error: str | None}

    Если GITEA_ADMIN_TOKEN не задан — возвращает ok=False с error.
    Не выбрасывает исключений (best-effort).
    """
    if not GITEA_ADMIN_TOKEN:
        return {"ok": False, "error": "GITEA_ADMIN_TOKEN not set in env"}

    username = _email_to_slug(email)
    temp_password = secrets.token_urlsafe(20)

    headers = {"Authorization": f"token {GITEA_ADMIN_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            # 1. Создать user — Gitea Admin API
            r = await cli.post(
                f"{GITEA_URL}/api/v1/admin/users",
                headers=headers,
                json={
                    "username": username,
                    "email": email,
                    "password": temp_password,
                    "full_name": full_name or username,
                    "must_change_password": True,
                    "send_notify": False,
                },
            )
            if r.status_code not in (201, 422):  # 422 = already exists
                logger.warning("gitea create_user %s failed %s: %s", username, r.status_code, r.text[:300])
                return {"ok": False, "username": username, "error": f"HTTP {r.status_code}"}
            user_existed = r.status_code == 422

            # 2. Создать org (приватный namespace для repos) — opt
            #    Org-name отличается от user-name префиксом «org-» чтобы
            #    избежать конфликта (Gitea: user и org в одном неймспейсе)
            org_name = f"{username}-team"
            r2 = await cli.post(
                f"{GITEA_URL}/api/v1/admin/users/{username}/orgs",
                headers=headers,
                json={
                    "username": org_name,
                    "full_name": f"{full_name or username}'s team",
                    "visibility": "private",
                    "repo_admin_change_team_access": True,
                },
            )
            # 422 = org уже существует — OK
            org_created = r2.status_code in (201, 422)

            return {
                "ok": True,
                "username": username,
                "temp_password": None if user_existed else temp_password,
                "org": org_name if org_created else None,
                "reused": user_existed,
                "url_login": f"{GITEA_URL.replace('http://cognitive_gitea:3000', 'https://git.me-ai.ru')}/user/login",
            }
    except Exception as e:
        logger.exception("gitea create_user %s exception: %s", username, e)
        return {"ok": False, "username": username, "error": f"{type(e).__name__}: {e}"}


async def ensure_org_for_owner(email: str, full_name: str | None = None) -> dict:
    """Idempotent — вызывается после OTP email-verify.

    Если Gitea недоступен — silently skip (welcome покажет alert).
    """
    if not await is_gitea_alive():
        logger.info("gitea not alive — skipping ensure_org for %s", email)
        return {"ok": False, "error": "gitea not deployed/up yet"}
    return await create_user(email, full_name)
