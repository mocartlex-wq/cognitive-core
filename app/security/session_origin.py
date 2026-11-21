"""Origin check for writes that use the cabinet's ambient session cookie."""
from fastapi import HTTPException, Request

from app.security.session import SESSION_COOKIE_NAME


def require_same_origin_write(request: Request) -> None:
    if not request.cookies.get(SESSION_COOKIE_NAME):
        return  # require_user already verified the explicit session header
    origin = request.headers.get("origin", "")
    host = request.url.netloc
    site = request.headers.get("sec-fetch-site")
    # Production sessions use Secure cookies; the TLS proxy can present HTTP
    # to the app. Do not trust an HTTP origin there just because ASGI sees HTTP.
    loopback = request.url.hostname in ("localhost", "127.0.0.1", "::1")
    local_origin = request.url.scheme == "http" and origin == f"http://{host}" and (loopback or site == "same-origin")
    same_origin = origin == f"https://{host}" or local_origin
    if site not in (None, "same-origin") or not same_origin:
        raise HTTPException(403, "Действие доступно из кабинета владельца")
