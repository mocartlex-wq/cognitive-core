FROM python:3.11-slim

WORKDIR /app

# Workaround для хостов без IPv6 outbound (типичный сценарий для VPS в РФ):
# deb.debian.org возвращает только AAAA-запись через анткаст Fastly, и если
# у хоста нет IPv6 → apt падает с "Unable to connect to deb.debian.org:80".
# Принудительно используем IPv4 + переключаем на mirror.yandex.ru (быстрее
# в РФ, работает и из остальных стран). На хостах с IPv6 — тоже корректно.
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list; \
    fi && \
    echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
