# SRE Runbook — Cognitive Core

> **Production**: https://mcp.me-ai.ru (legacy alias https://mcp.ии-память.рф)
> **Server**: cognitive-core-server (Tailscale `100.81.77.25`, SSH key `~/.ssh/cogcore_lan salex@`)
> **Repo**: github.com/me-ai-ru/cognitive-core
> **Auto-deploy**: systemd-timer 60s → git fetch → conditional_reload → smoke-test → rollback if fail

Этот документ — единый source-of-truth для on-call инцидентов и рутинного обслуживания. Каждый раздел построен по схеме **Симптом → Diagnostic → Resolution → Verification**.

---

## Quick reference

| Симптом | Раздел |
|---|---|
| `502 Bad Gateway` / `503 Service Unavailable` на mcp.me-ai.ru | [1. Production down](#1-production-down-502503-on-mcpme-airu) |
| Auto-deploy висит / `cognitive-deploy.service failed` | [2. Auto-deploy failed](#2-auto-deploy-failed) |
| `df -h` → 85%+ на root partition | [3. Disk usage > 85%](#3-disk-usage--85) |
| `psql: connection refused` или 502 после Postgres restart | [4. Postgres down / connection pool exhausted](#4-postgres-down--connection-pool-exhausted) |
| `room_*` MCP tools timeout / 502 на /rooms | [5. cognitive-rooms.service not responding](#5-cognitive-roomsservice-not-responding) |
| Browser warning о protected/expired cert | [6. SSL certificate expired](#6-ssl-certificate-expired) |
| MinIO 500/AccessDenied, missing objects | [7. MinIO bucket issues](#7-minio-bucket-issues) |
| `cogcore-uploads` заполняет диск | [8. Stuck upload directory](#8-stuck-upload-directory-tmpcogcore-uploads) |
| Watchdog email/log spam про `cognitive_mcp` | [9. Watchdog false alerts](#9-watchdog-false-alerts) |
| Auto-deploy блокируется на dirty tree | [10. Auto-deploy dirty tree](#10-auto-deploy-dirty-tree-recovery) |

---

## Incident response procedures

### 1. Production down (502/503 on mcp.me-ai.ru)

**Симптом**: внешний пользователь видит nginx error page, MCP-клиенты дисконнектят.

**Diagnostic**:
```bash
# 1. Reachability (с любой машины)
curl -fsS https://mcp.me-ai.ru/health
# ожидаемо: {"status":"ok","checks":{...}}

# 2. На сервере — что отдает nginx
sudo docker exec cognitive_nginx curl -s http://cognitive_api:8000/health

# 3. Логи nginx upstream errors
sudo docker logs --tail=200 cognitive_nginx | grep -E "(upstream|error|connect)"

# 4. Статус всех контейнеров
sudo docker ps --format "table {{.Names}}\t{{.Status}}"

# 5. Memory pressure / OOMKilled
sudo dmesg | grep -i "killed process" | tail -5
sudo docker inspect cognitive_api --format='{{.State.OOMKilled}}'
```

**Likely причины (по убыванию частоты)**:
1. `cognitive_api` упал после Postgres recreate (см. раздел 4)
2. OOM на cognitive_api (memory limit; недавно поднимали 3G → 8G)
3. Nginx-контейнер потерял DNS resolve до upstream (после `up -d` без `--force-recreate`)
4. cognitive_postgres недоступна — pool exhausted
5. Disk full → write failures → API возвращает 5xx

**Resolution**:
```bash
# Если только api лёг
sudo docker restart cognitive_api
sleep 5
curl -fsS https://mcp.me-ai.ru/health

# Если nginx не видит upstream
sudo docker compose -f /opt/cognitive-core/docker-compose.yml up -d --force-recreate nginx

# Если несколько контейнеров — полный stack restart (last resort)
cd /opt/cognitive-core
sudo docker compose restart api nginx
# postgres/redis/minio НЕ restart-ить без отдельного диагноза (см. раздел 4)
```

**Verification**:
```bash
# Health должен вернуть 200 и checks: postgres/redis/minio = ok
curl -fsS https://mcp.me-ai.ru/health | jq .

# Smoke-test основных эндпойнтов
curl -fsS https://mcp.me-ai.ru/agents/online
curl -fsS https://mcp.me-ai.ru/rooms
```

---

### 2. Auto-deploy failed

**Симптом**: новый коммит в main не появляется в проде через 2-3 минуты; `systemctl status cognitive-deploy.service` → failed.

**Diagnostic**:
```bash
# Live логи последнего запуска
sudo journalctl -u cognitive-deploy.service -n 200 --no-pager

# История запусков (последние 24h)
sudo journalctl -u cognitive-deploy.service --since "24h ago" | grep -E "(Started|Failed|exit)"

# Состояние git workdir
sudo -u root git -C /opt/cognitive-core status -sb
sudo -u root git -C /opt/cognitive-core log --oneline -5
```

**Common cause #1: Docker IPv6 issue (registry-1.docker.io unreachable из РФ)**:

Логи покажут что-то вроде:
```
Get "https://registry-1.docker.io/v2/": dial tcp [2600:...]: connect: network is unreachable
```

Это **самая частая** причина в РФ-сегменте. Docker daemon резолвит docker.io по AAAA-записи, но IPv6 не настроен/блокирован.

**Workaround (immediate)**:
```bash
# Pull через CLI (он fallback-ает на IPv4)
sudo docker pull python:3.11-slim
sudo docker pull nginx:alpine
# затем повторить deploy вручную
sudo systemctl start cognitive-deploy.service
sudo journalctl -u cognitive-deploy.service -f
```

**Permanent fix** (рекомендуется):
```bash
# Отключить IPv6 в docker daemon
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "ipv6": false,
  "log-driver": "json-file",
  "log-opts": {"max-size": "100m", "max-file": "3"}
}
EOF
sudo systemctl restart docker
# проверка
docker info | grep -i ipv6
```
После рестарта Docker → все контейнеры поднимутся заново (~1-2 мин downtime). Планировать в окно.

**Common cause #2: dirty tree** — см. [раздел 10](#10-auto-deploy-dirty-tree-recovery).

**Common cause #3: smoke-test fail после deploy**:
Auto-deploy запускает smoke-test (5 из 6 health checks должны быть OK), и если fail → автоматический rollback. Логи покажут `rollback initiated`. После rollback версия в проде = previous commit. Расследовать причину fail отдельно (обычно баг в новом коммите → revert PR).

**Verification**:
```bash
# Текущий SHA в проде
sudo -u root git -C /opt/cognitive-core rev-parse HEAD
# Должен совпадать с github.com/me-ai-ru/cognitive-core main

# Timer работает
sudo systemctl status auto-deploy.timer
sudo systemctl list-timers | grep cognitive
```

---

### 3. Disk usage > 85%

**Симптом**: watchdog alert / `df -h` показывает root partition >85% / API возвращает 5xx из-за write fail.

**Diagnostic**:
```bash
# Общая картина по mount points
df -h | grep -vE "(tmpfs|udev|loop)"

# Top-20 дисковых "пожирателей" в /
sudo du -h --max-depth=2 / 2>/dev/null | sort -rh | head -20

# Docker конкретно (build cache, images, volumes, logs)
sudo docker system df -v | head -50
```

**Likely причины**:
- **Docker build cache** (исторически до 147 GB) — главный виновник
- **Docker volumes** (postgres data, minio) — нормальный рост
- **/var/log/journal/** — systemd journal
- **/tmp/cogcore-uploads** (см. раздел 8)
- **Cold tier** `/mnt/cold/cognitive-snapshots/` — backup retention 90d

**Resolution — manual prune**:
```bash
# Безопасный prune: только unused build cache (НЕ трогает running)
sudo docker builder prune -af --filter "until=72h"

# Чуть агрессивнее: + dangling images + unused networks
sudo docker system prune -af --filter "until=72h" --volumes=false

# !!! НЕ ЗАПУСКАТЬ !!! без backup verification:
# sudo docker system prune -af --volumes   # удалит anonymous volumes — может задеть postgres/minio

# Освобождение journal
sudo journalctl --vacuum-size=2G
sudo journalctl --vacuum-time=30d
```

**Verify weekly cron работает**:
```bash
# Должен быть установлен: cogcore-docker-prune.timer (FIX из репо, не enabled по умолчанию)
sudo systemctl status cogcore-docker-prune.timer

# Если inactive — enable
sudo systemctl enable --now cogcore-docker-prune.timer
sudo systemctl list-timers | grep prune
# ожидаемо: NEXT = ближайшее воскресенье 04:00 UTC
```

Schedule: **Sun 04:00 UTC** — еженедельно очищает Docker build cache, не задевая running images / volumes.

**Verification**:
```bash
df -h /
# должно быть <80% после prune
sudo docker system df
# Build Cache → reclaimable должно быть малое число
```

---

### 4. Postgres down / connection pool exhausted

**Симптом**: api возвращает 502/503, в логах `cognitive_api` — `asyncpg.PostgresConnectionError` / `too many connections` / `pool timeout`.

**Diagnostic**:
```bash
# Контейнер жив?
sudo docker ps | grep cognitive_postgres

# Postgres отвечает на ping
sudo docker exec cognitive_postgres pg_isready -U cognitive

# Текущие connections
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core \
  -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"

# Logs последний час
sudo docker logs --tail=300 cognitive_postgres | grep -iE "(error|fatal|panic)"

# Не запустилась репликация / corruption?
sudo docker logs cognitive_postgres 2>&1 | grep -iE "(corrupt|recovery|wal)" | tail -20
```

**КРИТИЧЕСКОЕ правило**: `docker compose up -d postgres` назначает Postgres-контейнеру **новый IP** в docker network. cognitive_api держит persistent asyncpg connection pool → все коннекшены становятся stale → API возвращает 502 даже когда Postgres уже встал.

**Resolution sequence** (порядок ВАЖЕН):
```bash
cd /opt/cognitive-core

# 1. Если Postgres крашнулся — поднять
sudo docker compose up -d postgres
# подождать ready
until sudo docker exec cognitive_postgres pg_isready -U cognitive; do sleep 2; done

# 2. ОБЯЗАТЕЛЬНО — restart api для пересоздания pool с новым IP
sudo docker compose restart api

# 3. ОБЯЗАТЕЛЬНО — force-recreate nginx (тоже кэширует upstream DNS)
sudo docker compose up -d --force-recreate nginx

# 4. Проверка
curl -fsS https://mcp.me-ai.ru/health | jq .checks.postgres
# должно быть "ok"
```

**Если pool просто исчерпан (без падения Postgres)**:
```bash
# Kill idle connections старше 5 минут
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < NOW() - INTERVAL '5 minutes'
  AND pid <> pg_backend_pid();
SQL

# Затем restart api
sudo docker restart cognitive_api
```

**Verification**:
```bash
# Все health checks зелёные
curl -fsS https://mcp.me-ai.ru/health | jq .

# Smoke-test write path
curl -fsS -X POST https://mcp.me-ai.ru/agents/online -d '{"agent_id":"healthcheck"}' \
  -H 'Content-Type: application/json'
```

---

### 5. cognitive-rooms.service not responding

**Симптом**: MCP `room_*` tools (room_ask, room_post, room_read) возвращают timeout/502; `/rooms` HTTP endpoint не отвечает.

**Важно**: cognitive-rooms.service — это **systemd service**, **не Docker контейнер**. Listens на `:9098`. Используется как backend для `room_*` MCP wrappers.

**Diagnostic**:
```bash
# Статус сервиса
sudo systemctl status cognitive-rooms.service

# Live логи
sudo journalctl -u cognitive-rooms.service -n 200 --no-pager

# Порт слушается?
sudo ss -tlnp | grep 9098

# Direct health на сервере
curl -fsS http://127.0.0.1:9098/health
```

**Resolution**:
```bash
# Простой restart
sudo systemctl restart cognitive-rooms.service
sleep 3
sudo systemctl status cognitive-rooms.service

# Если упал с ошибкой в логах — посмотреть код
sudo journalctl -u cognitive-rooms.service -p err -n 100 --no-pager
```

**Common gotcha — deploy drift**:
- В репо файл: `scripts/cognitive-rooms.py` (источник правды)
- На сервере запускается: `/usr/local/lib/cognitive-rooms.py` (то что systemd unit ссылается)
- `conditional_reload.sh` **автоматически синкает** их если файл попал в diff коммита.
- НО: если кто-то делал `cp` вручную на сервер без коммита в git → drift. Manual edits != reflected.

**Detect drift**:
```bash
diff /opt/cognitive-core/scripts/cognitive-rooms.py /usr/local/lib/cognitive-rooms.py
# должно быть empty
```

**Fix drift (если manual changes ценные — сначала закоммитить)**:
```bash
# Если на сервере есть unexpected changes которые не в git
sudo cp /usr/local/lib/cognitive-rooms.py /tmp/rooms-server-current.py
# diff против git
diff /opt/cognitive-core/scripts/cognitive-rooms.py /tmp/rooms-server-current.py
# Если нужно сохранить → создать PR с этими изменениями
# Если drift = старая ручная правка → перезаписать from git
sudo cp /opt/cognitive-core/scripts/cognitive-rooms.py /usr/local/lib/cognitive-rooms.py
sudo systemctl restart cognitive-rooms.service
```

**Никогда** не делать runtime edit прямо в `/usr/local/lib/cognitive-rooms.py` без PR — нарушение [No runtime edits before commit](C:\Users\mocar\.claude\projects\D-------------------1\memory\feedback_no_runtime_edits_before_commit.md) правила.

**Verification**:
```bash
sudo systemctl is-active cognitive-rooms.service
curl -fsS http://127.0.0.1:9098/health
# и через публичный домен
curl -fsS https://mcp.me-ai.ru/rooms
```

---

### 6. SSL certificate expired

**Симптом**: browser warning `NET::ERR_CERT_DATE_INVALID`, curl `certificate has expired`. Letsencrypt не renew-нулся.

**Diagnostic**:
```bash
# Когда expires
echo | openssl s_client -connect mcp.me-ai.ru:443 -servername mcp.me-ai.ru 2>/dev/null \
  | openssl x509 -noout -dates

# Все certs на сервере
sudo certbot certificates

# Last renew attempt logs
sudo journalctl -u certbot.timer -n 50 --no-pager
sudo cat /var/log/letsencrypt/letsencrypt.log | tail -100
```

**Domains under management**: `mcp.me-ai.ru`, `git.me-ai.ru`, `api.me-ai.ru`, `cloud.me-ai.ru`, и legacy IDN `mcp.ии-память.рф`.

**Resolution**:
```bash
# Dry-run чтобы убедиться что renew пройдёт
sudo certbot renew --dry-run

# Реальный renew
sudo certbot renew --no-random-sleep-on-renew

# Если certbot отдельно не делает reload nginx (зависит от deploy-hook):
sudo docker exec cognitive_nginx nginx -t
sudo docker exec cognitive_nginx nginx -s reload

# Если certbot fail с rate limit → подождать 7 дней
# Если fail с "challenge failed" → проверить что :80 reachable извне:
curl -fsS http://mcp.me-ai.ru/.well-known/acme-challenge/test
```

**Common gotchas**:
- Certbot challenge идёт через :80 → nginx должен слушать :80 и proxy `/.well-known/acme-challenge/` в acme-companion container.
- Если nginx container down → certbot fail. Сначала поднять nginx.

**Verification**:
```bash
# Новый expiry date (должно быть ~90 дней вперёд)
echo | openssl s_client -connect mcp.me-ai.ru:443 -servername mcp.me-ai.ru 2>/dev/null \
  | openssl x509 -noout -dates

# Browser test: открыть https://mcp.me-ai.ru — без warnings
```

---

### 7. MinIO bucket issues

**Симптом**: API возвращает ошибки при upload/download media, `mc admin info` показывает problems, объекты missing.

**КРИТИЧЕСКАЯ архитектурная особенность**: на сервере используется **один** MinIO bucket с именем `cognitive`, который делится между **тремя** логическими namespaces через path prefix:
- `l4/{owner_user_id}/...` — L4 snapshots (cognitive memory)
- `ai-crm/...` — CRM-приложение
- `ai-crm-public/...` — публичные ассеты CRM

**НИКОГДА** не делать `mc rb cognitive` (remove bucket) — это снесёт данные всех трёх систем сразу.

**Diagnostic**:
```bash
# Установить mc client alias (если ещё не)
sudo docker exec cognitive_minio mc alias set local http://localhost:9000 \
  "$(sudo docker exec cognitive_minio printenv MINIO_ROOT_USER)" \
  "$(sudo docker exec cognitive_minio printenv MINIO_ROOT_PASSWORD)"

# Healthcheck
sudo docker exec cognitive_minio mc admin info local

# Bucket exists и accessible?
sudo docker exec cognitive_minio mc ls local/cognitive/ | head -20

# Disk usage per prefix
sudo docker exec cognitive_minio mc du --depth 2 local/cognitive/
```

**Resolution scenarios**:

**a) MinIO container down**:
```bash
sudo docker compose -f /opt/cognitive-core/docker-compose.yml up -d minio
sudo docker logs --tail=100 cognitive_minio
```

**b) Bucket missing (после disk recovery)**:
```bash
# Создать заново (BUT data восстанавливать из backup отдельно)
sudo docker exec cognitive_minio mc mb local/cognitive
# Восстановить permissions
sudo docker exec cognitive_minio mc anonymous set download local/cognitive/ai-crm-public
```

**c) Restore single prefix из cold backup**:
```bash
# Backup лежит в /mnt/cold/cognitive-snapshots/minio/
ls /mnt/cold/cognitive-snapshots/minio/ | tail -10

# Restore (пример: L4 для конкретного owner)
sudo docker exec cognitive_minio mc mirror \
  /mnt/cold/cognitive-snapshots/minio/2026-05-26/l4/owner_xxx/ \
  local/cognitive/l4/owner_xxx/
```

**d) Object missing — diagnose**:
```bash
# Поиск по prefix
sudo docker exec cognitive_minio mc find local/cognitive --name "*pattern*"

# Audit access logs
sudo docker exec cognitive_minio mc admin logs local
```

**Verification**:
```bash
# Все три namespace доступны
sudo docker exec cognitive_minio mc ls local/cognitive/l4/ | head -3
sudo docker exec cognitive_minio mc ls local/cognitive/ai-crm/ | head -3
sudo docker exec cognitive_minio mc ls local/cognitive/ai-crm-public/ | head -3

# Smoke-test через API
curl -fsS https://mcp.me-ai.ru/health | jq .checks.minio
```

---

### 8. Stuck upload directory (/tmp/cogcore-uploads)

**Симптом**: `df -h /tmp` → 90%+, новые uploads возвращают 500, `/tmp/cogcore-uploads/` содержит multi-GB partial files.

**Background**: PR #108 ввёл resumable uploads (chunks накапливаются на диске до commit). PR #109 добавил hourly cleanup timer — но требует ручного `enable --now`.

**Diagnostic**:
```bash
du -sh /tmp/cogcore-uploads/
ls -la /tmp/cogcore-uploads/ | head -30

# Сколько файлов и сколько старше 24h
find /tmp/cogcore-uploads/ -type f -mmin +1440 | wc -l
find /tmp/cogcore-uploads/ -type f -mmin +1440 -printf "%s\n" | awk '{s+=$1} END {print s/1024/1024 " MB stale"}'

# Cleanup timer установлен?
sudo systemctl status cogcore-upload-cleanup.timer
```

**Resolution**:
```bash
# Immediate manual cleanup (старше 24h — safe, в самом худшем случае user перезаливает)
sudo find /tmp/cogcore-uploads/ -type f -mmin +1440 -delete

# Включить регулярный cleanup
sudo systemctl enable --now cogcore-upload-cleanup.timer
sudo systemctl list-timers | grep upload
# ожидаемо: NEXT — ближайший round hour
```

Cleanup script удаляет файлы старше 24 часов раз в час. Резюмируемые uploads с активной сессией имеют TTL > 24h только если клиент явно расширяет — для типовых случаев safe.

**Verification**:
```bash
sudo systemctl is-active cogcore-upload-cleanup.timer
df -h /tmp
# через час
sudo journalctl -u cogcore-upload-cleanup.service -n 20
```

---

### 9. Watchdog false alerts

**Симптом** (исторически, resolved 2026-05-27): `/var/log/cognitive-alerts.log` flooding с `cognitive_mcp container not found` каждые 5 минут.

**Root cause**: `cognitive-watchdog.sh` (cron `*/5`) проверял несуществующий `cognitive_mcp` контейнер — legacy имя, давно удалённое из docker-compose.

**Fix** (применен 2026-05-27):
```bash
# В репо: scripts/fix-watchdog-stale-container.sh
sudo bash /opt/cognitive-core/scripts/fix-watchdog-stale-container.sh

# Что делает: убирает cognitive_mcp из списка проверяемых, оставляя actual containers:
# cognitive_api cognitive_postgres cognitive_redis cognitive_minio
# cognitive_nginx cognitive_nats cognitive_gitea
```

**Verification**:
```bash
# Manual run watchdog — не должно быть ошибок про cognitive_mcp
sudo /usr/local/bin/cognitive-watchdog.sh
echo $?
# 0 = ok

# Проверить что log больше не flooding
sudo tail -50 /var/log/cognitive-alerts.log
sudo grep cognitive_mcp /var/log/cognitive-alerts.log | tail
# должно быть пусто или старые записи
```

**Если регрессия** (cron реверт случился): re-apply fix script.

---

### 10. Auto-deploy dirty tree recovery

**Симптом**: deploy не работает, `git status` в `/opt/cognitive-core/` показывает modified files (никто не коммитил). Логи `cognitive-deploy.service` показывают что-то типа `Your local changes would be overwritten by merge`.

**Root cause**: кто-то делал runtime `sed`/`cp`/`vim` правки прямо на сервере в git-tracked файлах вместо PR. Auto-deploy `git pull` отказывается перезаписывать diverged tree.

**Самопрофилактика**: **никогда** не редактировать файлы в `/opt/cognitive-core/` напрямую. Только через PR-flow → main → auto-deploy. [Правило в памяти](C:\Users\mocar\.claude\projects\D-------------------1\memory\feedback_no_runtime_edits_before_commit.md).

**Self-heal (PR `fix/mcp-wrappers` era / PR #87+)**: conditional_reload.sh теперь автоматически делает `git stash` + `git pull` + (если файл не был в diff коммита) попытка `stash pop`. Большинство dirty trees recover-ятся сами.

**Manual recovery если self-heal не справился**:
```bash
cd /opt/cognitive-core

# 1. Что именно изменено
sudo -u root git status -sb
sudo -u root git diff --stat

# 2. Спасти изменения (если они нужны — НЕ ОЧЕВИДНО, чаще всего runtime patch который надо переоформить в PR)
sudo -u root git diff > /tmp/runtime-changes-$(date +%s).patch
ls -la /tmp/runtime-changes-*.patch

# 3. Reset чтобы auto-deploy разблокировался
sudo -u root git reset --hard origin/main
sudo -u root git clean -fd  # удаляет untracked файлы — осторожно

# 4. Триггернуть deploy руками для проверки
sudo systemctl start cognitive-deploy.service
sudo journalctl -u cognitive-deploy.service -f
```

**Если в patch были осмысленные изменения** — оформить их как PR:
1. Локально склонировать repo
2. Применить patch: `git apply /tmp/runtime-changes-X.patch`
3. Создать branch, commit, push, открыть PR
4. После merge → auto-deploy подхватит legitimately

**Verification**:
```bash
sudo -u root git -C /opt/cognitive-core status -sb
# должно быть: ## main...origin/main, nothing to commit, working tree clean

sudo systemctl status cognitive-deploy.service
# active (waiting) — таймер ждёт следующий запуск
```

---

## Routine maintenance

### Daily checks (5 минут, можно автоматизировать)

```bash
# 1. Health endpoint должен возвращать all green
curl -fsS https://mcp.me-ai.ru/health | jq .
# Все checks: ok

# 2. Alerts log за последние 24h
sudo tail -100 /var/log/cognitive-alerts.log
sudo grep -c ERROR /var/log/cognitive-alerts.log
# Если новые — расследовать

# 3. Docker container restarts (>0 за день = подозрительно)
sudo docker ps --format "table {{.Names}}\t{{.Status}}"

# 4. Disk usage trend
df -h / /mnt/cold 2>/dev/null

# 5. Last auto-deploy successful
sudo systemctl status cognitive-deploy.service --no-pager
sudo journalctl -u cognitive-deploy.service --since "24h ago" | grep -c "FAILED"
```

### Weekly checks (15 минут)

```bash
# 1. Docker prune timer fires? (Sun 04:00 UTC)
sudo systemctl status cogcore-docker-prune.timer
sudo journalctl -u cogcore-docker-prune.service --since "8 days ago" | tail -30

# 2. Backup tier health
ls -la /mnt/cold/cognitive-snapshots/ | tail -10
# Должны быть свежие dated folders (≤7d hot, ≤90d cold)

sudo systemctl status cognitive-backup-tier.service
sudo journalctl -u cognitive-backup-tier.service --since "8 days ago" | grep -E "(complete|error)"

# 3. L1 prune работает? (TTL 7d processed events)
sudo journalctl -u cognitive-l1-prune.service --since "8 days ago" | tail -20

# 4. DeepSeek rule analyzer (Sun 04:00) — проверить новые предложения
sudo journalctl -u cogcore-rule-analyzer.service --since "8 days ago" | tail -30
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core \
  -c "SELECT id, status, created_at FROM rule_proposals WHERE created_at > NOW() - INTERVAL '7 days';"

# 5. Upload cleanup активен
sudo systemctl status cogcore-upload-cleanup.timer
du -sh /tmp/cogcore-uploads/
```

### Monthly checks (30 минут)

```bash
# 1. TLS certificate expiry (alert если <30 дней до expiry)
for d in mcp.me-ai.ru git.me-ai.ru api.me-ai.ru cloud.me-ai.ru; do
  exp=$(echo | openssl s_client -connect $d:443 -servername $d 2>/dev/null \
    | openssl x509 -noout -enddate | cut -d= -f2)
  echo "$d → expires: $exp"
done

# 2. Postgres vacuum/analyze status
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core <<'SQL'
SELECT schemaname, relname, last_autovacuum, last_autoanalyze, n_dead_tup
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 10;
SQL

# 3. Backup restore drill (раз в месяц — restore latest snapshot в тестовый mountpoint)
# выбрать недавний backup
latest=$(ls -t /mnt/cold/cognitive-snapshots/ | head -1)
echo "Testing restore: $latest"
# (детальная процедура — отдельный runbook, не в scope)

# 4. Security: secrets ротация
# - GitHub PAT для auto-merge (/etc/cognitive-deploy.env)
# - MinIO root credentials
# - Postgres password (изменение требует api restart)

# 5. Capacity planning
# Тренд disk usage
df -h /
du -sh /var/lib/docker/

# Тренд postgres размера
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core \
  -c "SELECT pg_size_pretty(pg_database_size('cognitive_core'));"

# Тренд MinIO
sudo docker exec cognitive_minio mc du local/cognitive/
```

---

## Escalation contacts

| Type | Contact | When |
|---|---|---|
| Owner | **mocartlex@yandex.ru** | Любой prolonged outage >30 мин, любые destructive операции |
| Owner backup | mocartlex@gmail.com | Если yandex недоступен (РКН issues) |
| Second opinion | DeepSeek via `scripts/delegate_deepseek.py freeform "..."` | Non-trivial решения, ambiguous root cause |
| Auto-deploy issues | Check PR history `gh pr list --repo me-ai-ru/cognitive-core --state merged --limit 20` | Identify recent change потенциально вызвавшее regression |
| Provider issues (домены, DNS) | Reg.ru / Cloudflare (зависит от домена) | Только если DNS/registrar level |

**DeepSeek pattern**:
```bash
# На сервере — задать strong opinion вопрос про инцидент
sudo python3 /opt/cognitive-core/scripts/delegate_deepseek.py freeform \
  "Postgres connection pool exhaustion at 14:00 UTC. logs: ... what's most likely root cause and 3 things to investigate first?"
```

---

## Quick command reference

### Container ops
```bash
# Listing
sudo docker ps                              # running
sudo docker ps -a                           # включая stopped
sudo docker images | head                   # local images

# Restart strategies
sudo docker restart cognitive_api           # быстро, IP сохраняется
sudo docker compose restart api             # быстро, через compose
sudo docker compose up -d --force-recreate nginx  # NEW container с новым IP
sudo docker compose up -d                   # noop если уже running

# Logs
sudo docker logs --tail=200 cognitive_api
sudo docker logs -f cognitive_api           # follow
sudo docker logs --since 1h cognitive_api | grep -i error

# Exec
sudo docker exec -it cognitive_postgres psql -U cognitive -d cognitive_core
sudo docker exec cognitive_api curl http://localhost:8000/health  # internal
```

### Systemd services
```bash
# Status
sudo systemctl status cognitive-deploy.service
sudo systemctl status cognitive-rooms.service
sudo systemctl list-timers | grep cogcore   # все наши таймеры

# Control
sudo systemctl restart cognitive-rooms.service
sudo systemctl enable --now cogcore-docker-prune.timer
sudo systemctl disable cogcore-rule-analyzer.timer  # временно отключить

# Logs
sudo journalctl -u cognitive-deploy.service -n 200 --no-pager
sudo journalctl -u cognitive-rooms.service -f       # follow
sudo journalctl --since "1h ago" -p err              # errors за час
```

### Postgres direct (gotcha: auto-mode часто блокирует)
```bash
# Quick query
sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core \
  -c "SELECT count(*) FROM events_l1;"

# Долгий interactive
sudo docker exec -it cognitive_postgres psql -U cognitive -d cognitive_core

# Helpful scripts вместо direct SQL (auto-mode friendly)
cogcore-search "owner:xxx tier:l1"
cogcore-bb list   # blackboard
cogcore-conv list owner_xxx
```

### Cogcore helper CLIs (`/usr/local/bin/`)
```bash
cogcore-bb           # L0 blackboard ops (read/write/list)
cogcore-search       # L1 multi-filter search
cogcore-presence     # NATS + L0 presence query
cogcore-memory-replay # replay events for debugging
cogcore-conv         # conversation history
cogcore-kg           # knowledge graph queries
cogcore-lock-mgr     # distributed locks inspect
```

### Auto-deploy manual trigger
```bash
# Force-run deploy сейчас (не ждать таймера)
sudo systemctl start cognitive-deploy.service

# Tail логов
sudo journalctl -u cognitive-deploy.service -f

# Что в репо vs prod
sudo -u root git -C /opt/cognitive-core fetch origin
sudo -u root git -C /opt/cognitive-core log HEAD..origin/main --oneline
```

### Health & smoke-tests
```bash
# Public
curl -fsS https://mcp.me-ai.ru/health | jq .
curl -fsS https://mcp.me-ai.ru/agents/online
curl -fsS https://mcp.me-ai.ru/rooms

# Internal (на сервере)
curl -fsS http://127.0.0.1:9098/health     # cognitive-rooms
sudo docker exec cognitive_api curl -fsS http://localhost:8000/health
sudo docker exec cognitive_postgres pg_isready -U cognitive
sudo docker exec cognitive_redis redis-cli PING
sudo docker exec cognitive_minio mc admin info local
```

### Backups & restore
```bash
# Hot tier (NVMe, ≤7d)
ls -lt /var/backups/cognitive/

# Cold tier (HDD, ≤90d)
ls -lt /mnt/cold/cognitive-snapshots/

# Manual snapshot
sudo /usr/local/bin/cogcore-snapshot.sh

# Verify integrity последнего
latest=$(ls -t /mnt/cold/cognitive-snapshots/ | head -1)
sudo /usr/local/bin/cogcore-backup-verify.sh /mnt/cold/cognitive-snapshots/$latest
```

---

## Appendix: Architecture cheatsheet

**Memory tiers**:
- **L0** (Redis): blackboard, quick recall, presence — TTL hours
- **L1** (Postgres `events_l1`): raw events, TTL 7d processed, prune daily 03:00
- **L2** (Postgres `events_l2`): daily summaries
- **L3** (Postgres `knowledge`): DeepSeek-curated, durable
- **L4** (MinIO `cognitive/l4/{owner}/`): snapshots, retention 90d

**Multi-tenant isolation**: `owner_user_id` column WHERE-фильтр на всех таблицах. 1 owner = много agents = общая память.

**Operating Rules (Phase 6)**: 5 core rules инжектятся в system_prompt:
1. Pre-answer recall (grep memory + cognitive_recall)
2. Post-task remember (dual write local+server)
3. Plan before non-trivial action
4. Mid-task patch (correct course based on feedback)
5. Media via pipeline (video/audio → cogmedia → frames → Read)

**Auto-deploy flow**:
```
GitHub main HEAD
  → systemd timer 60s
  → git fetch + diff
  → conditional_reload.sh (sync /usr/local/lib/* if needed)
  → docker compose up -d (only changed services)
  → smoke-test (5/6 health checks)
  → SUCCESS: keep | FAIL: git reset --hard previous + alert
```

---

*Last updated: 2026-05-27*
*Owner: mocartlex@yandex.ru | Repo: github.com/me-ai-ru/cognitive-core*
*Related docs: `AGENT_OPERATIONS.md` (in repo root), `docs/platform_capabilities_*.md` (in memory)*
