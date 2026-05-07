# Cognitive Core — Production Server Deployment

Полный путь от свежего Ubuntu сервера до работающей системы с TLS, бэкапами, мониторингом и remote MCP подключением для клиентов.

## Pre-flight checklist

| Что | Где взять |
|---|---|
| Ubuntu 22.04 / 24.04 LTS server | DigitalOcean / Hetzner / любой VPS |
| 4 CPU / 16 GB RAM / 100 GB SSD | минимум для small-to-medium use |
| Domain name (опционально, для Let's Encrypt) | nodaway.com / любой регистратор |
| DeepSeek API key | [platform.deepseek.com](https://platform.deepseek.com) |
| SSH доступ к серверу | стандартно при создании VPS |

## Шаг 1 — Подготовка сервера

```bash
# С локальной машины: подключиться
ssh root@your-server-ip

# Создать пользователя (если нужно)
adduser cognitive
usermod -aG sudo cognitive
su - cognitive
```

## Шаг 2 — One-command install

На сервере:

```bash
# Без домена (self-signed TLS)
git clone <repo-url> /opt/cognitive-core
cd /opt/cognitive-core
bash install-server.sh

# С доменом и Let's Encrypt
DOMAIN=cognitive.example.com EMAIL=admin@example.com \
  bash install-server.sh
```

`install-server.sh` за **~5-10 минут** делает:

| # | Что |
|---|---|
| 1 | apt: docker, docker-compose-plugin, curl, openssl, ufw |
| 2 | Клонирование репо в `/opt/cognitive-core` (если нужно) |
| 3 | `gen-secrets.sh` → strong passwords + интерактивный DeepSeek key prompt |
| 4 | Self-signed cert или Let's Encrypt (если задан DOMAIN) |
| 5 | UFW firewall: открыть 22/80/443, остальное закрыть |
| 6 | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` |
| 7 | systemd unit для auto-start на boot |
| 8 | Wait for healthy (60 попыток × 3 сек) |
| 9 | Печатает endpoints + agent API keys + connection инструкции |

## Шаг 3 — Проверка

После успешного install:

```bash
# Health через HTTPS
curl -k https://$DOMAIN/health   # или https://server-ip/health для self-signed

# Status
docker compose ps
sudo systemctl status cognitive-core

# Logs
docker compose logs -f --tail 50
```

В браузере: `https://$DOMAIN/` → должен открыться dashboard.

## Шаг 4 — Подключение клиентов

### Вариант A: HTTP/SSE через nginx (рекомендуется для production)

На **сервере** включаем MCP SSE режим:

```bash
docker exec -d cognitive_api python -m mcp_server.server --sse
# или добавить в docker-compose.prod.yml как side-car контейнер
```

На **клиенте** (Cherry Studio / Cursor):

```json
{
  "mcpServers": {
    "cognitive-core-remote": {
      "url": "https://$DOMAIN/mcp/sse",
      "transport": "sse",
      "headers": {
        "X-API-Key": "<agent_key из server .env>"
      }
    }
  }
}
```

### Вариант B: Local MCP proxy (если SSE не работает)

На клиенте установить `mcp-proxy`:

```bash
pip install mcp-proxy
```

Конфиг клиента:

```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "mcp-proxy",
      "args": ["--transport", "sse", "https://$DOMAIN/mcp/sse"],
      "env": { "X-API-Key": "<agent_key>" }
    }
  }
}
```

mcp-proxy на клиентской машине открывает stdio для Claude Desktop, проксирует к удалённому SSE.

## Шаг 5 — Бэкапы

`docker-compose.prod.yml` включает **автоматический backup-сервис**:

- Раз в **6 часов** делает `pg_dump` + `mc mirror` MinIO
- Складывает в `/opt/cognitive-core/backups/postgres/` и `/opt/cognitive-core/backups/minio/`
- **Ротация 14 дней** (старые удаляются)

Проверить что бэкапы делаются:

```bash
ls -la /opt/cognitive-core/backups/postgres/
tail /opt/cognitive-core/backups/backup.log
```

### Вынос бэкапов наружу (DR)

Cron на хосте (не в Docker) — раз в день копирует backups на S3/удалённый сервер:

```bash
0 4 * * * rsync -az /opt/cognitive-core/backups/ backup-host:/cognitive-backups/
```

## Шаг 6 — Восстановление (DR)

Если сервер упал и нужно поднять на новом:

```bash
# 1. Поднять стек как обычно
git clone <repo> /opt/cognitive-core
cd /opt/cognitive-core
bash install-server.sh

# 2. Скопировать бэкапы
rsync -az backup-host:/cognitive-backups/ /opt/cognitive-core/backups/

# 3. Restore Postgres
docker exec -i cognitive_postgres psql -U cognitive -d cognitive_core < <(gunzip -c /opt/cognitive-core/backups/postgres/latest.sql.gz)

# 4. Restore MinIO L4
docker exec cognitive_minio mc mirror /backups/minio/latest/ local/l4-snapshots/

# 5. Restore Redis vectors из pgvector (без LLM-вызовов)
curl -X POST -H "X-API-Key: <admin_key>" https://$DOMAIN/memory/restore-redis
```

## Шаг 7 — Resource limits

Production overlay (`docker-compose.prod.yml`) задаёт лимиты:

| Service | CPU | Memory |
|---|---|---|
| api | 1.0 | 2 GB |
| postgres | 1.0 | 2 GB |
| redis | 0.5 | 1 GB |
| minio | 0.5 | 1 GB |
| nginx | 0.5 | 256 MB |
| backup | 0.5 | 256 MB |

Итого ~7 GB / ~3.5 CPU — на 4 CPU / 16 GB RAM сервере остаётся запас 9 GB для burst.

## Шаг 8 — Logging

Docker logging driver `json-file` с ротацией:
- API: max 20 MB × 5 файлов = 100 MB
- Остальные: max 10 MB × 3 = 30 MB

Просмотр:
```bash
docker compose logs api --tail 200
docker compose logs postgres --since 1h
```

## Шаг 9 — Мониторинг (опционально)

См. [docker-compose.monitoring.yml](docker-compose.monitoring.yml) — добавляет Prometheus + Grafana stack.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.monitoring.yml up -d
# Prometheus: https://$DOMAIN/metrics
# Grafana:    https://$DOMAIN/grafana/
```

## Шаг 10 — GPU acceleration (опционально, для серверов с NVIDIA GPU)

Если на сервере есть NVIDIA GPU (например GTX 1050 Ti / RTX 3060 / Tesla T4) — fastembed
переключается на CUDA провайдер и embeddings ускоряются в 5-7 раз (15-30ms → 3-5ms).
Под текущую нагрузку (1000 events/день) выгода незаметна, но для multimodal v0.7
(локальный vision-OCR + Whisper) GPU становится обязательным.

### Подготовка хоста (Ubuntu 22.04)

```bash
# 1. NVIDIA driver (550 рекомендуется)
sudo apt install -y nvidia-driver-550
sudo reboot

# Проверить после ребута
nvidia-smi    # должен показать GPU и версию драйвера

# 2. NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Проверить интеграцию
docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi
```

### Запуск Cognitive Core с GPU

```bash
cd /opt/cognitive-core
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml \
  up -d --build
```

### Проверка что GPU реально используется

```bash
# В контейнере должен быть виден GPU
docker exec cognitive_api nvidia-smi

# /health должен сообщить provider=CUDA
curl -k https://$DOMAIN/health | jq '.embedding'
# {
#   "model": "intfloat/multilingual-e5-small",
#   "provider": "CUDA"
# }
```

### Минимальные требования по GPU

| Сценарий | Минимум | Рекомендую |
|---|---|---|
| fastembed (multilingual-e5-small) | 1 GB VRAM, CC 5.0+ | GTX 1050 Ti / RTX 2060 |
| Vision-LLM локально (v0.7) | 4 GB VRAM, CC 6.0+ | RTX 3060 12GB |
| Whisper-small для голоса | 2 GB VRAM | GTX 1660 / RTX 2070 |
| Локальный draft-LLM (Llama 3.2 3B Q4) | 4 GB VRAM | RTX 3060 12GB |

### Откат на CPU

Если что-то сломалось — просто запустить без GPU overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`EMBEDDING_USE_GPU` дефолтит в `false`, fastembed возвращается на CPU автоматически.

## Troubleshooting

| Симптом | Решение |
|---|---|
| install-server.sh падает на apt | `sudo apt update && sudo apt install -y docker.io` вручную, потом продолжить |
| Healthcheck не проходит >60s | `docker logs cognitive_api` — обычно DEEPSEEK_API_KEY не задан или ошибка миграций |
| HTTPS показывает self-signed warning | Это норма для self-signed. Используйте `-k` в curl или примите cert в браузере. Для production — Let's Encrypt |
| Cherry Studio не подключается через SSE | Проверьте: `curl -k https://$DOMAIN/mcp/sse` должен начать стрим. Если 404 — mcp_server в SSE режиме не запущен |
| OOM kill контейнеров | Уменьшите MAX_PAYLOAD_SIZE и L4_FULL_SNAPSHOT_INTERVAL_WEEKS, проверьте `docker stats` |
| Бэкапы не делаются | `docker logs cognitive_backup`. Часто проблема: cron не запустился — `docker exec cognitive_backup crontab -l` |

## Update procedure

```bash
cd /opt/cognitive-core
git fetch && git checkout v0.5.0
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker exec cognitive_api python -m pytest tests/ -q     # smoke test
```

При несовместимом обновлении схемы:
```bash
# Снапшот L3 → L4 перед миграцией
curl -X POST -H "X-API-Key: ..." https://$DOMAIN/memory/consolidate/weekly?domain=admin
git checkout v0.5.0
# Миграция выполнится автоматически при старте API (init_db с IF NOT EXISTS)
```

## Security checklist (обязательно для production)

- [ ] `.env` с правами 600 (`chmod 600 /opt/cognitive-core/.env`)
- [ ] `gen-secrets.sh` запущен — пароли НЕ дефолтные
- [ ] `AGENT_API_KEYS` каждый — длинный hex (32 байта = 64 символа)
- [ ] UFW активен, открыты только 22/80/443
- [ ] TLS-сертификат настроен (self-signed для теста, Let's Encrypt для prod)
- [ ] Бэкапы делаются (`tail backups/backup.log`)
- [ ] Бэкапы копируются НАРУЖУ (rsync на DR-сервер)
- [ ] Auto-renewal Let's Encrypt: `systemctl status certbot.timer`
- [ ] Monitoring: alerting на 5xx и `auth_failure` rate
- [ ] systemd unit активен: `systemctl is-enabled cognitive-core`

## Связанные документы

- [`README.md`](README.md) — обзор проекта
- [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — интеграция агентов с памятью
- [`CHERRY_STUDIO.md`](CHERRY_STUDIO.md) — подключение из Cherry Studio
- [`mcp_server/QUICKSTART.md`](mcp_server/QUICKSTART.md) — настройка MCP-клиента
- [`DEPLOY.md`](DEPLOY.md) — детальный runbook (расширенная версия)
