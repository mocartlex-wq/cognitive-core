"""Serve repo docs/*.md files over HTTP.

QA (2026-05-28) обнаружил P2: ссылки https://mcp.me-ai.ru/docs/concepts.md в
README, claim-промпте (connect.py), agent-discovery.md отдавали 404 — `/docs`
это FastAPI Swagger UI, а markdown-файлы не serv-ились. Агенты по onboarding
получали 404 на документацию.

Fix: route `/docs/{doc_name}` (param) сосуществует с Swagger `/docs` (exact).
Serve raw markdown (text/markdown) — агенты читают через curl, браузер покажет
текст. Security: только .md, no path traversal.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["docs"])

# docs/ в корне репо. __file__ = app/api/docs_serve.py → parents[2] = repo root
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


@router.get("/docs/{doc_name}", response_class=PlainTextResponse)
async def serve_markdown_doc(doc_name: str) -> PlainTextResponse:
    """Отдать markdown-файл из docs/ как text/markdown.

    Сосуществует с FastAPI Swagger UI на `/docs` (exact path) — этот route
    ловит только `/docs/<что-то>`.
    """
    # Security: только .md, никакого path traversal
    if not doc_name.endswith(".md") or "/" in doc_name or "\\" in doc_name or ".." in doc_name:
        raise HTTPException(status_code=404, detail="not found")

    path = DOCS_DIR / doc_name
    if not path.is_file():
        # Не раскрываем структуру — generic 404 + список доступных
        try:
            available = sorted(p.name for p in DOCS_DIR.glob("*.md"))
        except Exception:
            available = []
        raise HTTPException(
            status_code=404,
            detail={"error": f"doc '{doc_name}' not found", "available": available[:30]},
        )

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("docs serve read failed %s: %s", doc_name, e)
        raise HTTPException(status_code=500, detail="read error")

    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


@router.get("/docs-list")
async def list_docs() -> dict:
    """JSON-список доступных markdown-документов (для discovery)."""
    try:
        docs = sorted(p.name for p in DOCS_DIR.glob("*.md"))
    except Exception:
        docs = []
    return {"count": len(docs), "docs": docs, "base_url": "/docs/"}
