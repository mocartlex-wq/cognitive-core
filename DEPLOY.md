# Cognitive Core — Production Deploy Runbook

Инструкция по развёртыванию в production: Linux + Docker + TLS + бэкапы + мониторинг.

## Минимальные требования

| Компонент | Dev | Production |
|---|---|---|
| OS | Любая с Docker | Ubuntu 22.04 / 24.04 LTS |
| CPU | 2 core | 4-8 core |
| RAM | 4 GB | 16-32 GB |
| Disk | 10 GB | 100 GB SSD + 500 GB для L4 (S3) |
| Docker | Docker Desktop | Docker Engine + Compose v2 |
| TLS | необязательно | обязательно (nginx + Let's Encrypt) |
| Бэкапы | необязательно | обязательно (cron + S3) |

## Архитектура production

```
Internet
   │ (HTTPS :443)
   ▼
┌────────────────────────────────────────┐
│ nginx (TLS termination, rate-limit)    │
└────────┬───────────────────────────────┘
         │ proxy_pass http://api:8000
         ▼
┌────────────────────────────────────────┐
│ cognitive_api (FastAPI)                │
└─────┬──────────┬──────────┬────────────┘
      │          │          │
   ┌──▼──┐  ┌────▼───┐  ┌───▼────┐
   │ PG  │  │ Redis  │  │ MinIO  │
   │ +pgv│  │ Stack  │  │ (S3)   │
   └─────┘  └────────┘  └────────┘
       │ pg_dump cron       │ s3-mirror
       ▼                    ▼
   /backups/postgres/   external S3
```

## Шаги деплоя

### 1. Сервер: подготовка

```bash
# Ubuntu 22.04 / 24.04
sudo apt update && sudo apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx ufw

# UFW: открыть только web-порты, остальное закрыть
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# Не выставлять наружу 5432/6379/9000 — внутренние сервисы
```

### 2. Получить код и .env

```bash
sudo mkdir -p /opt/cognitive-core
sudo chown $USER:$USER /opt/cognitive-core
cd /opt/cognitive-core
git clone <repo-url> .

cp .env.example .env
# Отредактировать .env:
#   DEEPSEEK_API_KEY=sk-...      ← вставить рабочий ключ
#   AGENT_API_KEYS={"prod_agent":"длинный-случайный-ключ"}
#   DATABASE_URL=postgresql://cognitive:НОВЫЙ_СЕКРЕТ@postgres:5432/cognitive_core
#   S3_ACCESS_KEY=новый, S3_SECRET_KEY=новый
chmod 600 .env  # никто кроме owner не читает
```

### 3. Postgres — usuń дефолтный пароль

В `docker-compose.prod.yml` пароль уже берётся из `.env`. Установите свой:

```bash
# Сгенерировать пароль
openssl rand -base64 32
# вставить в .env как POSTGRES_PASSWORD и в DATABASE_URL
```

### 4. TLS-сертификат

**Вариант А: Let's Encrypt (рекомендуется для public domain)**

```bash
sudo certbot --nginx -d cognitive.example.com
# certbot создаст конфиг и подключит сертификат
# Автообновление: systemctl status certbot.timer
```

**Вариант B: self-signed (для интранета или теста)**

```bash
mkdir -p nginx/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/certs/server.key \
  -out nginx/certs/server.crt \
  -subj "/CN=cognitive.local"
```

### 5. Запуск production-стека

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
# Все 5 контейнеров healthy, включая nginx
```

Проверка:
```bash
curl -k https://cognitive.local/health
# {"healthy":true,...}
```

### 6. Бэкапы Postgres

Скрипт уже есть: `scripts/backup_postgres.sh`. Установить cron:

```bash
sudo crontab -e
# Каждые 6 часов
0 */6 * * * /opt/cognitive-core/scripts/backup_postgres.sh >> /var/log/cognitive-backup.log 2>&1
```

Бэкапы складываются в `/opt/cognitive-core/backups/postgres/` с ротацией (хранятся 14 дней).

Для копирования наружу:
```bash
# Cron каждый день: rsync на удалённый storage
0 4 * * * rsync -az /opt/cognitive-core/backups/ backup-server:/cognitive-backups/
```

### 7. MinIO L4 — копирование наружу

L4-снапшоты живут в MinIO. Для DR нужно копировать в внешнее S3:

```bash
# Установить mc (MinIO client)
wget https://dl.min.io/client/mc/release/linux-amd64/mc -O /usr/local/bin/mc
chmod +x /usr/local/bin/mc

# Настроить алиасы
mc alias set local http://localhost:9000 minioadmin <S3_SECRET_KEY_из_env>
mc alias set remote https://s3.example.com PUBLIC_KEY SECRET_KEY

# Cron: ежедневная синхронизация
0 5 * * * mc mirror --overwrite local/l4-snapshots remote/cognitive-backups/l4
```

### 8. Мониторинг

#### Health-check
```bash
# Простой watchdog
*/5 * * * * curl -sf https://cognitive.local/health > /dev/null || \
  curl -X POST https://hooks.slack.com/... -d 'text=Cognitive Core DOWN'
```

#### Prometheus
- Метрики на `/metrics` (без auth — закройте через nginx если публично)
- Grafana дашборд (TODO: prepare-config в `monitoring/`)

#### Алерты на ошибки
Дашборд → Аудит L5 → фильтр «Только ошибки». В production — настроить alert на рост `auth_failure` или `validation_error`.

### 9. Обновление версии

```bash
cd /opt/cognitive-core
git fetch && git checkout v0.5.0  # или новый тег
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker exec cognitive_api alembic upgrade head  # если новая миграция
docker exec cognitive_api python -m pytest tests/ -q  # smoke-test
```

При downtime-обнволениях — рассмотрите rolling update через 2+ инстанса API за nginx (advisory lock защитит от двойной консолидации).

### 10. Откат

```bash
# Если новая версия глючит:
git checkout v0.4.0
docker compose up -d --build api
docker exec cognitive_api alembic downgrade -1  # если миграция мешает

# Если данные испорчены — восстановить L3 из L4 snapshot
curl -X POST https://cognitive.local/memory/snapshots/restore/<snapshot_id> \
  -H "X-API-Key: $ADMIN_KEY"
```

### 11. DR — полный disaster recovery

Если упал весь сервер:

1. Поднять новый сервер (шаги 1-3)
2. Восстановить Postgres из бэкапа:
   ```bash
   docker compose up -d postgres
   docker exec -i cognitive_postgres psql -U cognitive -d cognitive_core < backups/postgres/latest.sql
   ```
3. Восстановить MinIO L4 из remote-S3:
   ```bash
   mc mirror remote/cognitive-backups/l4 local/l4-snapshots
   ```
4. Запустить остальной стек: `docker compose up -d`
5. Restore Redis-векторов из pgvector (без LLM):
   ```bash
   curl -X POST https://cognitive.local/memory/restore-redis -H "X-API-Key: ..."
   ```

## Безопасность checklist

- [ ] `.env` не в git, права 600
- [ ] `AGENT_API_KEYS` — длинные случайные значения (не дефолтные `key-design-001`)
- [ ] `POSTGRES_PASSWORD` сгенерирован, не дефолт
- [ ] `S3_ACCESS_KEY/SECRET_KEY` не minioadmin
- [ ] UFW: открыты только 22/80/443
- [ ] TLS включён, certbot auto-renewal
- [ ] `/metrics` закрыт от публичного доступа (через nginx allow only от Prometheus)
- [ ] Бэкапы Postgres + MinIO работают и копируются наружу
- [ ] Алерты на 5xx и `auth_failure` rate
- [ ] Логи API ротируются (Docker logging driver)

## Troubleshooting

### `cognitive_api` падает

```bash
docker logs cognitive_api --tail 100
# Чаще всего: DEEPSEEK_API_KEY не задан или истёк
```

### KNN-поиск возвращает пусто после рестарта Redis

Векторы в Redis имеют TTL. После рестарта восстанавливаются из pgvector:

```bash
curl -X POST https://cognitive.local/memory/restore-redis -H "X-API-Key: ..."
```

### Daily/weekly не запускаются автоматически

Worker встроен в `cognitive_api`. Проверить:
```bash
docker logs cognitive_api | grep scheduler
```

В UI: дашборд → Аудит L5 — должны быть события `daily_consolidate` каждый день.

### Postgres «collation version mismatch»

Появляется при переходе с `postgres:16` на `pgvector/pgvector:pg16`. Безопасно для работы, но можно исправить:

```bash
docker exec cognitive_postgres psql -U cognitive -d cognitive_core -c \
  "ALTER DATABASE cognitive_core REFRESH COLLATION VERSION;"
```

### MinIO console не открывается

Порт 9002 — это console MinIO. `http://server:9002`, логин `minioadmin` или из `.env`.

### Двойная консолидация (lock_held)

Это норма при N>1 инстансах API — advisory lock защищает от дублей. Если хочется убедиться — параллельно вызовите два `/memory/consolidate/daily?domain=X` — один вернёт `lock_held`.

### Пересоздать индекс Redis после смены модели эмбеддингов

```bash
curl -X POST https://cognitive.local/memory/reindex -H "X-API-Key: ..."
# Удаляет stale-векторы (с другим model_version) и переиндексирует все домены
```

## Метрики которые стоит мониторить

| Метрика | Где брать | Норма |
|---|---|---|
| Healthy services | `GET /health` `services:{}` | все ok |
| Размер L1 | `layers.l1` | растёт но cleanup держит ≤ 14 дней |
| Размер L3 | `layers.l3_knowledge + l3_tools` | растёт медленно (десятки/сотни в день) |
| Размер БД | `db_size_mb` | <1 GB для среднего использования |
| LLM success rate | Prometheus `cognitive_llm_calls_total` | >99% |
| HTTP latency p95 | Prometheus `cognitive_http_request_duration` | < 100 ms (без LLM) |
| Audit failures | `GET /dashboard/audit-tail?only_failures=true` | 0 |

## Стоимость

Грубая оценка для small-tenant (1000 событий/день, 1 worker daily, 1 weekly):
- DeepSeek: ~$1-3/мес
- VPS (4 CPU / 16 GB): $20-40/мес
- S3 хранение L4 (если внешний): копейки
- Embedding (CPU): бесплатно (fastembed локально)

Для больших нагрузок — переход на Ollama + GPU (этап v1.0).
