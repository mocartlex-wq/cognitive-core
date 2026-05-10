# Tiny image for the rooms + pg-to-nats sidecars.
# Lighter than the api image; only the deps these scripts actually use.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build deps for psycopg[binary] are already wheels — no compile step.
RUN pip install --no-cache-dir \
        "psycopg[binary]==3.2.3" \
        "httpx==0.27.2" \
        "fastapi==0.115.6" \
        "uvicorn[standard]==0.32.1" \
        "redis==5.2.0" \
        "openai==1.55.0"

# Scripts get mounted at /app/extras at runtime via compose.
WORKDIR /app
