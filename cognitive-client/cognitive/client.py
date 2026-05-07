"""Cognitive Core client — sync and async API for the 5-layer memory system.

Usage:
    async with AsyncMemoryClient("http://localhost:9001", api_key="key-...") as mem:
        await mem.remember("codegen", {"task": "..."})
        ctx = await mem.query("codegen", "How to handle errors?")
        # Session auto-closes with keep_results=True on context exit
"""

import json
import os
import time
import random
from typing import Optional, Any

# ---- Async Client ----

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


class CognitiveError(Exception):
    """Base exception for Cognitive Core API errors."""
    pass


class CognitiveAPIError(CognitiveError):
    """API returned an error response."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"[{status_code}] {detail}")


class CognitiveTimeout(CognitiveError):
    """Request timed out."""
    pass


class AsyncMemoryClient:
    """Async client for Cognitive Core API.

    Usage:
        async with AsyncMemoryClient("http://localhost:9001", api_key="key-...") as mem:
            await mem.remember("codegen", {"task": "...", "result": "..."})
            ctx = await mem.query("codegen", "How to handle errors?")
            # On exit: session auto-closed, results fed back to L1
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        domain: str = "default",
        timeout: float = 60.0,
        max_retries: int = 3,
        auto_close_keep: bool = True,
    ):
        self.base_url = base_url or os.getenv("COGNITIVE_URL", "http://localhost:9001")
        self.api_key = api_key or os.getenv("COGNITIVE_API_KEY", "default-key")
        self.domain = domain
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_close_keep = auto_close_keep
        self._client: Optional[httpx.AsyncClient] = None
        self._active_session: Optional[str] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *args):
        if self._active_session:
            try:
                await self.close_session(keep_results=self.auto_close_keep)
            except Exception:
                pass
        if self._client:
            await self._client.aclose()

    def _req(self) -> httpx.AsyncClient:
        if self._client is None:
            # Auto-create client for non-context-manager usage
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        return self._client

    async def _retry_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Exponential backoff retry loop for transient errors."""
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                r = await self._req().request(method, path, **kwargs)
                if r.status_code < 500:
                    return r
                # 5xx: retryable
                last_exc = CognitiveAPIError(r.status_code, r.text[:200])
            except httpx.TimeoutException as e:
                last_exc = CognitiveTimeout(str(e))
            except httpx.ConnectError as e:
                last_exc = CognitiveError(f"Connection failed: {e}")

            if attempt < self.max_retries - 1:
                wait = (2 ** attempt) * 0.5 + random.uniform(0, 0.5)
                await _asleep(wait)

        if last_exc:
            raise last_exc
        raise CognitiveError(f"{method} {path} failed after {self.max_retries} retries")

    async def _get(self, path: str, **kwargs) -> dict | list:
        r = await self._retry_request("GET", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    async def _post(self, path: str, **kwargs) -> dict:
        r = await self._retry_request("POST", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    async def _delete(self, path: str, **kwargs) -> dict:
        r = await self._retry_request("DELETE", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    # ---- Core API ----

    async def health(self) -> dict:
        """Check API health. Returns {healthy, services, layers}."""
        return await self._get("/health")

    async def metrics(self) -> str:
        """Get Prometheus metrics as text."""
        r = await self._req().get("/metrics")
        return r.text

    async def remember(
        self, payload: dict, domain: str = "", source_agent: str = ""
    ) -> dict:
        """Ingest an event into L1. Returns {id, status, timestamp}."""
        data = {
            "source_agent": source_agent or "cognitive-sdk",
            "domain": domain or self.domain,
            "payload": payload,
        }
        return await self._post("/events", json=data)

    async def remember_batch(
        self, events: list[dict], domain: str = "", source_agent: str = ""
    ) -> list[dict]:
        """Ingest multiple events. Returns list of {id, status, timestamp}."""
        results = []
        for payload in events:
            r = await self.remember(payload, domain, source_agent)
            results.append(r)
        return results

    async def query(
        self, query: str, domain: str = "", top_k: int = 5, include_tools: bool = True
    ) -> dict:
        """KNN search in L3. Creates an OP session. Returns {session_id, domain, results}."""
        data = {
            "domain": domain or self.domain,
            "context": query,
            "top_k": top_k,
            "include_tools": include_tools,
        }
        result = await self._post("/operative/query", json=data)  # type: ignore[arg-type]
        self._active_session = result.get("session_id")
        return result

    async def close_session(
        self,
        session_id: str = "",
        keep_results: bool = True,
        results_summary: dict | None = None,
        source_agent: str = "",
    ) -> dict:
        """Close an operative session, optionally feeding results back to L1."""
        sid = session_id or self._active_session
        if not sid:
            return {"status": "error", "detail": "No active session"}
        data = {
            "session_id": sid,
            "keep_results": keep_results,
            "results_summary": results_summary,
            "source_agent": source_agent or "cognitive-sdk",
        }
        result = await self._post(f"/operative/sessions/{sid}/close", json=data)
        if sid == self._active_session:
            self._active_session = None
        return result

    async def feedback(
        self, record_id: str, useful: bool, record_type: str = "knowledge"
    ) -> dict:
        """Record feedback on a specific OP result."""
        if not self._active_session:
            return {"status": "error", "detail": "No active session"}
        data = {
            "session_id": self._active_session,
            "record_id": record_id,
            "record_type": record_type,
            "useful": useful,
        }
        return await self._post(
            f"/operative/sessions/{self._active_session}/feedback", json=data
        )

    # ---- Memory management ----

    async def consolidate_daily(
        self, domain: str = "", since_hours: int | None = None
    ) -> dict:
        """Trigger daily L1→L2 consolidation."""
        d = domain or self.domain
        params = f"?domain={d}"
        if since_hours is not None:
            params += f"&since_hours={since_hours}"
        return await self._post(f"/memory/consolidate/daily{params}")

    async def consolidate_weekly(self, domain: str = "") -> dict:
        """Trigger weekly L2→L3 consolidation with vector indexing."""
        d = domain or self.domain
        return await self._post(f"/memory/consolidate/weekly?domain={d}")

    async def audit(self, domain: str = "") -> dict:
        """Run monthly L3 audit (staleness, duplicates, conflicts)."""
        d = domain or self.domain
        return await self._post(f"/memory/audit/monthly?domain={d}")

    async def cleanup(self) -> dict:
        """Delete L1 events older than retention_days."""
        return await self._post("/memory/cleanup")

    # ---- Snapshots ----

    async def snapshots(self, domain: str | None = None) -> list[dict]:
        """List L4 snapshots."""
        params = f"?domain={domain}" if domain else ""
        return await self._get(f"/memory/snapshots{params}")  # type: ignore[return-value]

    async def restore_snapshot(self, snapshot_id: str) -> dict:
        """Restore L3 from an L4 snapshot."""
        return await self._post(f"/memory/snapshots/restore/{snapshot_id}")

    # ---- Tools ----

    async def register_tool(
        self,
        tool_name: str,
        tool_type: str = "service",
        domain: str = "",
        description: str = "",
        config_schema: dict | None = None,
        usage_patterns: dict | None = None,
    ) -> dict:
        """Register a new tool in the L3 registry."""
        data = {
            "domain": domain or self.domain,
            "tool_name": tool_name,
            "tool_type": tool_type,
            "description": description,
            "config_schema": config_schema or {},
            "usage_patterns": usage_patterns or {},
        }
        return await self._post("/tools", json=data)

    async def list_tools(self, domain: str = "") -> dict:
        """List active tools for a domain."""
        d = domain or self.domain
        return await self._get(f"/tools?domain={d}")  # type: ignore[return-value]

    async def deprecate_tool(self, tool_id: str) -> dict:
        """Mark a tool as deprecated (soft delete)."""
        return await self._delete(f"/tools/{tool_id}")

    # ---- Convenience: session as context ----

    async def session(
        self, query: str, domain: str = "", top_k: int = 5
    ):
        """Context manager for a query session. Auto-closes on exit.

        Usage:
            async with mem.session("How to handle errors?", "codegen") as ctx:
                for item in ctx["results"]:
                    ...
        """
        return _AsyncSessionContext(self, query, domain, top_k)


class _AsyncSessionContext:
    def __init__(self, client: AsyncMemoryClient, query: str, domain: str, top_k: int):
        self.client = client
        self.query = query
        self.domain = domain
        self.top_k = top_k
        self.result: dict = {}

    async def __aenter__(self) -> dict:
        self.result = await self.client.query(self.query, self.domain, self.top_k)
        return self.result

    async def __aexit__(self, *args):
        await self.client.close_session(keep_results=True)


async def _asleep(seconds: float):
    """Async sleep wrapper."""
    import asyncio
    await asyncio.sleep(seconds)


# ---- Sync Client ----

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class MemoryClient:
    """Sync client for Cognitive Core API.

    Usage:
        mem = MemoryClient("http://localhost:9001", api_key="key-...")
        mem.remember("codegen", {"task": "...", "result": "..."})
        ctx = mem.query("codegen", "How to handle errors?")
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        domain: str = "default",
        timeout: float = 60.0,
        max_retries: int = 3,
        auto_close_keep: bool = True,
    ):
        self.base_url = base_url or os.getenv("COGNITIVE_URL", "http://localhost:9001")
        self.api_key = api_key or os.getenv("COGNITIVE_API_KEY", "default-key")
        self.domain = domain
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_close_keep = auto_close_keep
        self._session: Optional[requests.Session] = None
        self._active_session: Optional[str] = None

    def __enter__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        })
        self._session.timeout = self.timeout
        return self

    def __exit__(self, *args):
        if self._active_session:
            try:
                self.close_session(keep_results=self.auto_close_keep)
            except Exception:
                pass
        if self._session:
            self._session.close()
            self._session = None

    def _sess(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            })
            self._session.timeout = self.timeout
        return self._session

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _retry_request(self, method: str, path: str, **kwargs) -> requests.Response:
        last_exc = None
        url = self._url(path)
        for attempt in range(self.max_retries):
            try:
                r = self._sess().request(method, url, **kwargs)
                if r.status_code < 500:
                    return r
                last_exc = CognitiveAPIError(r.status_code, r.text[:200])
            except requests.Timeout as e:
                last_exc = CognitiveTimeout(str(e))
            except requests.ConnectionError as e:
                last_exc = CognitiveError(f"Connection failed: {e}")

            if attempt < self.max_retries - 1:
                wait = (2 ** attempt) * 0.5 + random.uniform(0, 0.5)
                time.sleep(wait)

        if last_exc:
            raise last_exc
        raise CognitiveError(f"{method} {path} failed after {self.max_retries} retries")

    def _get(self, path: str, **kwargs) -> dict | list:
        r = self._retry_request("GET", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    def _post(self, path: str, **kwargs) -> dict:
        r = self._retry_request("POST", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    def _delete(self, path: str, **kwargs) -> dict:
        r = self._retry_request("DELETE", path, **kwargs)
        if r.status_code >= 400:
            raise CognitiveAPIError(r.status_code, r.text[:200])
        return r.json()

    # ---- Core API ----

    def health(self) -> dict:
        return self._get("/health")  # type: ignore[return-value]

    def metrics(self) -> str:
        return self._sess().get(self._url("/metrics")).text

    def remember(self, payload: dict, domain: str = "", source_agent: str = "") -> dict:
        data = {
            "source_agent": source_agent or "cognitive-sdk",
            "domain": domain or self.domain,
            "payload": payload,
        }
        return self._post("/events", json=data)

    def remember_batch(self, events: list[dict], domain: str = "", source_agent: str = "") -> list[dict]:
        return [self.remember(payload, domain, source_agent) for payload in events]

    def query(self, query: str, domain: str = "", top_k: int = 5, include_tools: bool = True) -> dict:
        data = {
            "domain": domain or self.domain,
            "context": query,
            "top_k": top_k,
            "include_tools": include_tools,
        }
        result = self._post("/operative/query", json=data)
        self._active_session = result.get("session_id")
        return result

    def close_session(self, session_id: str = "", keep_results: bool = True,
                      results_summary: dict | None = None, source_agent: str = "") -> dict:
        sid = session_id or self._active_session
        if not sid:
            return {"status": "error", "detail": "No active session"}
        data = {
            "session_id": sid, "keep_results": keep_results,
            "results_summary": results_summary,
            "source_agent": source_agent or "cognitive-sdk",
        }
        r = self._post(f"/operative/sessions/{sid}/close", json=data)
        if sid == self._active_session:
            self._active_session = None
        return r

    def feedback(self, record_id: str, useful: bool, record_type: str = "knowledge") -> dict:
        if not self._active_session:
            return {"status": "error", "detail": "No active session"}
        data = {"session_id": self._active_session, "record_id": record_id,
                "record_type": record_type, "useful": useful}
        return self._post(f"/operative/sessions/{self._active_session}/feedback", json=data)

    # ---- Memory management ----

    def consolidate_daily(self, domain: str = "", since_hours: int | None = None) -> dict:
        d = domain or self.domain
        params = f"?domain={d}"
        if since_hours is not None:
            params += f"&since_hours={since_hours}"
        return self._post(f"/memory/consolidate/daily{params}")

    def consolidate_weekly(self, domain: str = "") -> dict:
        d = domain or self.domain
        return self._post(f"/memory/consolidate/weekly?domain={d}")

    def audit(self, domain: str = "") -> dict:
        d = domain or self.domain
        return self._post(f"/memory/audit/monthly?domain={d}")

    def cleanup(self) -> dict:
        return self._post("/memory/cleanup")

    # ---- Snapshots ----

    def snapshots(self, domain: str | None = None) -> list[dict]:
        params = f"?domain={domain}" if domain else ""
        return self._get(f"/memory/snapshots{params}")  # type: ignore[return-value]

    def restore_snapshot(self, snapshot_id: str) -> dict:
        return self._post(f"/memory/snapshots/restore/{snapshot_id}")

    # ---- Tools ----

    def register_tool(self, tool_name: str, tool_type: str = "service",
                      domain: str = "", description: str = "",
                      config_schema: dict | None = None,
                      usage_patterns: dict | None = None) -> dict:
        data = {
            "domain": domain or self.domain, "tool_name": tool_name,
            "tool_type": tool_type, "description": description,
            "config_schema": config_schema or {},
            "usage_patterns": usage_patterns or {},
        }
        return self._post("/tools", json=data)

    def list_tools(self, domain: str = "") -> dict:
        d = domain or self.domain
        return self._get(f"/tools?domain={d}")  # type: ignore[return-value]

    def deprecate_tool(self, tool_id: str) -> dict:
        return self._delete(f"/tools/{tool_id}")

    def close(self):
        if self._session:
            self._session.close()
            self._session = None
