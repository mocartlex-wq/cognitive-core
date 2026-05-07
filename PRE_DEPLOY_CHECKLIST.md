# Pre-Deploy Checklist

Короткий чек-лист перед `bash install-server.sh` на сервере.

## За день до переноса (на dev-машине)

- [ ] `git pull` — latest version в локальном репо
- [ ] `docker exec cognitive_api python -m pytest tests/ -q` → 114+ passing
- [ ] `python scripts/stress_test.py` → PASS
- [ ] `git log --oneline | head -3` — последние commits OK
- [ ] `git tag --list` → видна `v0.5.0-rc1` (или новее)

## На сервере (Ubuntu 22/24)

### Подготовка

- [ ] Создан VPS Ubuntu 22.04 / 24.04 LTS
- [ ] Минимум 4 CPU / 16 GB RAM / 100 GB SSD
- [ ] SSH доступ работает
- [ ] (Опционально) DNS A-record `cognitive.example.com` → IP сервера
- [ ] (Опционально) Email для Let's Encrypt

### Получены секреты

- [ ] DeepSeek API key (`sk-...`) от platform.deepseek.com
- [ ] Будут сгенерированы: POSTGRES_PASSWORD, S3_ACCESS_KEY, S3_SECRET_KEY, 3 agent keys

## Установка (одна команда)

```bash
ssh user@server
git clone <repo-url> /opt/cognitive-core
cd /opt/cognitive-core
DOMAIN=cognitive.example.com EMAIL=admin@example.com bash install-server.sh
```

Или без домена (self-signed TLS):

```bash
bash install-server.sh
```

## После install (ожидаем "Cognitive Core deployed!")

- [ ] `curl -k https://localhost/health` → `"healthy":true`
- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` → все 6 контейнеров healthy/running
- [ ] `sudo systemctl is-enabled cognitive-core` → enabled
- [ ] `sudo ufw status` → 22, 80, 443 ALLOW
- [ ] `ls -la /opt/cognitive-core/.env` → `-rw------` (permission 600)
- [ ] `ls /opt/cognitive-core/nginx/certs/` → server.crt + server.key
- [ ] Через 10 минут: `ls -la /opt/cognitive-core/backups/postgres/` → есть файл (или ждать 6h)

## Подключение клиентов

### Cherry Studio / Cursor (remote MCP)

```json
{
  "mcpServers": {
    "cognitive-core-remote": {
      "url": "https://cognitive.example.com/mcp/sse",
      "transport": "sse",
      "headers": {
        "X-API-Key": "<agent_key из server .env>"
      }
    }
  }
}
```

- [ ] В клиенте: статус MCP — running (зелёный)
- [ ] В чате: «Используй cognitive_health» → JSON ответ

## Если что-то пошло не так

| Симптом | Команда |
|---|---|
| API не healthy | `docker logs cognitive_api --tail 50` |
| Healthcheck timeout | `docker compose ps` — все ли started |
| TLS warning в браузере | self-signed cert — это норма, нажмите "Advanced → Proceed" |
| 502 Bad Gateway | nginx запустился до api, подождите 30 сек или `docker compose restart nginx` |
| Бэкапы не идут | `docker logs cognitive_backup` (cron запускается каждые 6 часов) |
| MCP остаётся failed | См. mcp-server-cognitive-core.log в Claude Desktop logs (на клиентской машине) |

## Полный сброс если нужно

```bash
cd /opt/cognitive-core
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
rm -rf .env nginx/certs/
bash install-server.sh
```

⚠️ `down -v` удаляет volumes — все данные памяти потеряются. Перед этим:

```bash
# 1. Бэкап
docker exec cognitive_backup /usr/local/bin/cron-backup.sh

# 2. Скопировать бэкап наружу
scp /opt/cognitive-core/backups/postgres/latest.sql.gz dev-machine:~/

# 3. Только потом — сброс
```

## После первой недели работы

- [ ] `dogfooding/` собрал реальные friction в `friction.md`
- [ ] `cc-daily` (через ssh) работает
- [ ] Бэкапы наружу через rsync настроены
- [ ] Let's Encrypt cert auto-renewal проверен: `sudo systemctl status certbot.timer`
- [ ] Решено что добавить из `roadmap.md` v0.5+

## Связанные документы

- [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md) — детальный runbook
- [`SECURITY.md`](SECURITY.md) — security checklist
- [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — подключение клиентов
- [`CHERRY_STUDIO.md`](CHERRY_STUDIO.md) — Cherry Studio setup
