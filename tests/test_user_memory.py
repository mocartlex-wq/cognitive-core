from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api import user_memory as memory
from app.security.session import SESSION_COOKIE_NAME


@pytest.mark.asyncio
async def test_agent_key_cannot_access_owner_memory():
    app = FastAPI()
    app.include_router(memory.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cabinet.test") as client:
        rid = uuid4()
        for method, path in [("GET", ""), ("GET", f"/{rid}"), ("GET", f"/{rid}/comments"),
                             ("POST", f"/{rid}/comments")]:
            response = await client.request(method, "/user/memory/records" + path,
                                            headers={"X-Api-Key": "not-an-owner-session"},
                                            json={"request_id": str(uuid4()), "text": "review"} if method == "POST" else None)
            assert response.status_code == 401


@pytest.mark.parametrize("origin,site,allowed", [
    ("http://cabinet.test", None, False), ("http://cabinet.test", "same-origin", True),
    ("https://cabinet.test", None, True), ("https://cabinet.test", "same-origin", True),
    ("https://foreign.test", "same-origin", False), ("null", "cross-site", False),
    (None, None, False), ("https://cabinet.test", "same-site", False),
])
def test_cookie_write_origin(origin, site, allowed):
    headers = {"host": "cabinet.test", "cookie": f"{SESSION_COOKIE_NAME}=session"}
    if origin:
        headers["origin"] = origin
    if site:
        headers["sec-fetch-site"] = site
    request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                       "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
    if allowed:
        memory.require_same_origin_write(request)
    else:
        with pytest.raises(HTTPException, match="403"):
            memory.require_same_origin_write(request)


def test_cursor_preserves_ties_and_refuses_bad_input():
    stamp = datetime.now(timezone.utc)
    ids = [uuid4(), uuid4()]
    page = memory._page([{"id": rid, "timestamp": stamp} for rid in ids], 1, "timestamp")
    assert memory._decode_cursor(page["next_cursor"]) == (stamp, ids[0])
    for bad in ("", "bad!", "////"):
        with pytest.raises(HTTPException, match="422"):
            memory._decode_cursor(bad)
