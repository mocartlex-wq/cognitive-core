"""
Drop-in CORS middleware for cognitive-rooms.py.

Loose defaults for self-host (allow all origins, common headers + verbs).
Tighten via env in production.

Usage
-----
    from rooms_cors_patch import install_cors
    install_cors(app)

ENV
---
    ROOMS_CORS_ORIGINS   default "*"      (comma-separated, or "*")
    ROOMS_CORS_METHODS   default "GET,POST,OPTIONS,DELETE,PATCH"
    ROOMS_CORS_HEADERS   default "Authorization,Content-Type,X-Room-Key,X-API-Key"
    ROOMS_CORS_MAX_AGE   default "86400"
    ROOMS_CORS_CREDS     default "false"  ("true" enables Allow-Credentials)
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware


def install_cors(app: FastAPI) -> None:
    origins_env = os.environ.get("ROOMS_CORS_ORIGINS", "*")
    if origins_env == "*":
        origins = ["*"]
    else:
        origins = [o.strip() for o in origins_env.split(",") if o.strip()]

    methods = [m.strip() for m in os.environ.get(
        "ROOMS_CORS_METHODS", "GET,POST,OPTIONS,DELETE,PATCH"
    ).split(",")]

    headers = [h.strip() for h in os.environ.get(
        "ROOMS_CORS_HEADERS", "Authorization,Content-Type,X-Room-Key,X-API-Key"
    ).split(",")]

    max_age = int(os.environ.get("ROOMS_CORS_MAX_AGE", "86400"))
    creds = os.environ.get("ROOMS_CORS_CREDS", "false").lower() == "true"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=methods,
        allow_headers=headers,
        allow_credentials=creds,
        max_age=max_age,
    )
