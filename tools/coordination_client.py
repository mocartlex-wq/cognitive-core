"""Small dependency-free client for the authoritative Core task ledger.

Run with ``python -m tools.coordination_client --help``. Credentials are read
only from the process environment, never accepted as command-line arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.client import HTTPException
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class CoordinationError(RuntimeError):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class NoRedirect(HTTPRedirectHandler):
    """Do not forward an API credential to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CoordinationClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 20):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("API URL must be an http(s) origin without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Non-loopback Core API connections require HTTPS")
        if not api_key or any(c in api_key for c in "\r\n"):
            raise ValueError("COGCORE_COORD_API_KEY or COGCORE_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._opener = build_opener(NoRedirect())

    @classmethod
    def from_env(cls, base_url: str | None = None) -> CoordinationClient:
        return cls(base_url or os.environ.get("COGCORE_COORD_API_URL", "http://127.0.0.1:9001"),
                   os.environ.get("COGCORE_COORD_API_KEY") or os.environ.get("COGCORE_API_KEY", ""))

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + "/coord/tasks" + path, data=data, method=method,
                          headers={"X-API-Key": self._api_key, "Accept": "application/json",
                                   "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            # Limit diagnostics and redact even if a misconfigured server echoes a header.
            try:
                detail = json.loads(exc.read(16_384)).get("detail", "request_rejected")
            except (ValueError, AttributeError, OSError, HTTPException):
                detail = "request_rejected"
            raise CoordinationError(str(detail).replace(self._api_key, "[redacted]")[:2000], exc.code) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise CoordinationError("Core API connection failed or timed out") from None
        except (ValueError, UnicodeError):
            raise CoordinationError("Core API returned invalid JSON", 502) from None
        if not isinstance(result, dict):
            raise CoordinationError("Core API returned a non-object response", 502)
        return result

    def list(self, *, project: str | None = None, status: str | None = None,
             limit: int = 200, cursor: str | None = None) -> dict:
        params = {k: v for k, v in {"project": project, "status": status,
                  "limit": limit, "cursor": cursor}.items() if v is not None}
        return self._request("GET", "?" + urlencode(params))

    def iter_tasks(self, *, project: str | None = None, status: str | None = None) -> Iterator[dict]:
        cursor = None
        seen = set()
        while True:
            page = self.list(project=project, status=status, cursor=cursor)
            yield from page["tasks"]
            cursor = page.get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise CoordinationError("Core API repeated a pagination cursor", 502)
            seen.add(cursor)

    def get(self, task_id: str) -> dict:
        return self._request("GET", "/" + str(UUID(task_id)))

    def history(self, task_id: str, *, before_version: int | None = None, limit: int = 200) -> dict:
        if not 1 <= limit <= 200 or (before_version is not None and before_version < 1):
            raise ValueError("History limit must be 1..200 and before_version must be positive")
        params = {"limit": limit}
        if before_version is not None:
            params["before_version"] = before_version
        return self._request("GET", "/" + str(UUID(task_id)) + "/history?" + urlencode(params))

    def iter_history(self, task_id: str, *, before_version: int | None = None,
                     limit: int = 200) -> Iterator[dict]:
        """Read all matching events newest first, following the version cursor."""
        cursor = before_version
        while True:
            page = self.history(task_id, before_version=cursor, limit=limit)
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and (type(next_cursor) is not int or next_cursor < 1
                                            or (cursor is not None and next_cursor >= cursor)):
                raise CoordinationError("Core API returned a non-decreasing or invalid history cursor", 502)
            # Core returns each page oldest first, but pages themselves go backwards.
            yield from reversed(page["events"])
            if next_cursor is None:
                return
            cursor = next_cursor

    def create(self, body: dict) -> dict:
        return self._request("POST", "", body)

    def action(self, task_id: str, body: dict) -> dict:
        return self._request("POST", "/" + str(UUID(task_id)) + "/actions", body)


def _read_json(source: str | None) -> dict[str, Any]:
    if source is None:
        return {}
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--json must contain a JSON object")
    return data


def _merge(body: dict, **fields) -> dict:
    for key, value in fields.items():
        if value is not None:
            if key in body and body[key] != value:
                raise ValueError(f"Conflicting {key} in JSON and command-line/environment")
            body[key] = value
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Core origin; defaults to COGCORE_COORD_API_URL")
    parser.add_argument("--session-id", default=os.environ.get("COGCORE_COORD_SESSION_ID") or os.environ.get("COGCORE_SESSION_ID"))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--project")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=200)
    listing.add_argument("--cursor")
    listing.add_argument("--all", action="store_true", help="Follow every next_cursor")
    get = commands.add_parser("get")
    get.add_argument("task_id")
    get.add_argument("--history", action="store_true")
    get.add_argument("--all-history", action="store_true", help="Read every older history page; output oldest first")
    get.add_argument("--before-version", type=int, help="History events before this exclusive version cursor")
    get.add_argument("--limit", type=int, default=200, help="History page size, 1..200 (default 200)")
    create = commands.add_parser("create")
    create.add_argument("--json", required=True, help="JSON file or - for stdin")
    create.add_argument("--idempotency-key")
    action = commands.add_parser("action")
    action.add_argument("task_id")
    action.add_argument("action", choices=["claim", "heartbeat", "recover", "submit", "block", "fail", "retry", "cancel", "review", "assign"])
    action.add_argument("--expected-version", required=True, type=int)
    action.add_argument("--idempotency-key", required=True)
    action.add_argument("--attempt-id")
    action.add_argument("--json", help="Additional fields, JSON file or - for stdin")
    args = parser.parse_args(argv)
    try:
        client = CoordinationClient.from_env(args.url)
        if args.command == "list":
            result = ({"tasks": list(client.iter_tasks(project=args.project, status=args.status))} if args.all else
                      client.list(project=args.project, status=args.status, limit=args.limit, cursor=args.cursor))
        elif args.command == "get":
            if args.all_history:
                events = list(client.iter_history(args.task_id, before_version=args.before_version, limit=args.limit))
                result = {"events": list(reversed(events)), "next_cursor": None}
            elif args.history:
                result = client.history(args.task_id, before_version=args.before_version, limit=args.limit)
            elif args.before_version is not None or args.limit != 200:
                raise ValueError("--before-version and --limit require --history or --all-history")
            else:
                result = client.get(args.task_id)
        else:
            body = _merge(_read_json(args.json), session_id=args.session_id, idempotency_key=args.idempotency_key)
            if not body.get("session_id") or not body.get("idempotency_key"):
                raise ValueError("Mutations require explicit session_id and idempotency_key")
            if args.command == "create":
                result = client.create(body)
            else:
                body = _merge(body, action=args.action, expected_version=args.expected_version, attempt_id=args.attempt_id)
                result = client.action(args.task_id, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CoordinationError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "http_status": getattr(exc, "status", None)}, ensure_ascii=False), file=sys.stderr)
        return 2 if isinstance(exc, ValueError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
