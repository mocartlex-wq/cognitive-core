# Upgrade Guide

Как безопасно обновлять Cognitive Core между версиями.

## General principle

**Всегда: backup → upgrade → verify → rollback if needed.**

```
Текущая (работает) → Backup → git pull новой версии → docker compose up --build
   ↓
Verify (tests + health + sample queries)
   ↓
OK → готово.   FAIL → docker compose down + restore from backup → git checkout старой версии
```

## Перед обновлением

Всегда:

```bash
cd /opt/cognitive-core   # на сервере

# 1. Снять backup до обновления
docker exec cognitive_backup /usr/local/bin/cron-backup.sh

# 2. Запомнить текущую версию (на случай rollback)
git log -1 --format="%H %s"

# 3. Прочитать CHANGELOG.md в новой версии — есть ли breaking changes
git fetch && git log HEAD..origin/main --oneline
```

## Pattern: Minor upgrade (0.5.0 → 0.5.1)

Без breaking changes:

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker exec cognitive_api python -m pytest tests/ -q   # smoke test
curl -k https://localhost/health                       # верификация
```

Время: ~3 минуты.

## Pattern: Major upgrade (0.5.x → 0.6.0)

С возможными breaking changes (новые таблицы, изменения схемы):

```bash
# 1. Backup как обычно
docker exec cognitive_backup /usr/local/bin/cron-backup.sh

# 2. Прочитать CHANGELOG для миграционных шагов
git fetch && git log HEAD..origin/main --oneline | head

# 3. Pull
git pull

# 4. Если есть Alembic миграции — Alembic запустит автоматически при старте API
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 5. Verify
docker exec cognitive_api python -m pytest tests/ -q
curl -k https://localhost/health

# 6. Если что-то странное — откат
git checkout <previous-tag>
docker compose up -d --build
docker exec -i cognitive_postgres psql -U cognitive -d cognitive_core < <(gunzip -c backups/postgres/latest.sql.gz)
```

## Версии и breaking changes

| From → To | Breaking changes | Migration |
|---|---|---|
| 0.4.x → 0.5.0 | + agent_states table, + MCP SSE transport | Auto via init_db (CREATE IF NOT EXISTS) |
| 0.3.x → 0.4.0 | + pgvector extension, + L3 embedding column | Требуется Postgres с pgvector. Используйте `pgvector/pgvector:pg16` image |
| 0.2.x → 0.3.0 | Postgres advisory locks, новые ENV vars | Re-run gen-secrets.sh, переписать .env |
| pre-0.2 | Не поддерживается | Свежая установка |

## Откат (rollback) — если новая версия сломалась

```bash
cd /opt/cognitive-core

# 1. Stop сервис
docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# 2. git rollback
git checkout v0.4.0   # или предыдущий рабочий tag

# 3. Restore Postgres из бэкапа (если данные испорчены)
docker compose up -d postgres
sleep 10
docker exec -i cognitive_postgres psql -U cognitive -d cognitive_core \
  < <(gunzip -c /opt/cognitive-core/backups/postgres/latest.sql.gz)

# 4. Restart всё
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
sleep 10
curl -k https://localhost/health
```

## Hot-reload без downtime (для будущего)

В планах v0.6+:
- Multi-instance API за nginx (advisory lock уже защищает от race)
- Rolling update: stop одного instance, обновить, start, повторить для второго
- Zero downtime для пользователей

В **v0.5** — нет hot-reload, downtime ~30 секунд при `docker compose up --build`.

## Security upgrades (срочные)

Если вышел security fix:

```bash
git fetch
git log HEAD..origin/main --grep="SECURITY" --oneline   # есть ли security commits
```

При SECURITY commits — обновляйте **немедленно**:

```bash
docker exec cognitive_backup /usr/local/bin/cron-backup.sh
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Подписаться на security advisories (когда будет публичный репо):
`https://github.com/<repo>/security/advisories`

## После upgrade — что проверить

- [ ] `curl -k https://localhost/health` → `"healthy":true`
- [ ] `docker compose ps` → все контейнеры running
- [ ] `docker exec cognitive_api python -m pytest tests/ -q` → 114+ passing
- [ ] Sample query: `cognitive_recall` через MCP вернул данные
- [ ] Логи без ERROR: `docker compose logs --tail 50 | grep -i error`
- [ ] Backup-сервис активен: `docker compose ps cognitive_backup`

## Обновление .env (новые переменные)

Иногда новая версия требует новых ENV:

```bash
# 1. Сравнить с .env.production.example
diff <(grep "^[A-Z]" .env | sort) <(grep "^[A-Z]" .env.production.example | sort) | head

# 2. Добавить недостающие из примера в свой .env (с осторожностью)
```

## Связанные документы

- [`CHANGELOG.md`](CHANGELOG.md) — все изменения
- [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md) — full runbook
- [`SECURITY.md`](SECURITY.md) — как реагировать на security advisories
