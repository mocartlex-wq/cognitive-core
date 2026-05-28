FROM python:3.11-slim

WORKDIR /app

# Workaround для хостов без IPv6 outbound (типичный сценарий для VPS в РФ
# и для security-strict on-prem машин агентов): deb.debian.org отдаёт только
# AAAA-запись через анткаст Fastly, и если у хоста нет IPv6 → apt падает с
# "Unable to connect to deb.debian.org:80". Принудительно IPv4 + mirror.yandex.ru
# (быстрее в РФ, работает и из остальных стран). На хостах с IPv6 — тоже корректно.
# Зеркалит паттерн из корневого Dockerfile (app api), чтобы поведение совпадало.
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list; \
    fi && \
    echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 && \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# faster-whisper (CTranslate2 backend, CPU int8) + ffmpeg-python обёртка +
# FastAPI стек. Тяжёлые ML-веса НЕ кладём в образ — модель скачивается лениво
# в named volume /data/whisper при первом /process/* запросе (см. main.py).
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "pydantic>=2.6" \
    "httpx>=0.27" \
    "faster-whisper>=1.0" \
    "ffmpeg-python>=0.2"

COPY main.py .

EXPOSE 9099

# Healthcheck бьёт по локальному /health (см. main.py). Контейнер слушает
# 0.0.0.0:9099 ВНУТРИ сети контейнера; наружу публикуется только на 127.0.0.1
# через docker-compose (localhost-only bind — security). curl установлен выше.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9099/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9099"]
