# Quickstart: Self-hosted установка за 30 минут

## Зачем self-host

- **Полная независимость**: ваши данные не покидают вашу инфраструктуру (152-ФЗ / GDPR / GxP / банковская тайна)
- **Кастомизация**: подключение собственных LLM, расширение модулей, патчи под ваш use-case
- **Air-gap**: можно поставить в изолированной сети без интернета (для on-premise LLM)
- **SLA вашей инфры**: 100% контроль uptime, никакой зависимости от mcp.me-ai.ru

## Требования

| Компонент | Минимум | Рекомендация |
|---|---|---|
| **CPU** | 2 cores | 4-8 cores |
| **RAM** | 4 GB | 16 GB |
| **Storage** | 50 GB SSD | 200 GB NVMe + холодный HDD под бэкапы |
| **OS** | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 LTS |
| **Docker** | 24+ | 27+ с compose v2 |
| **Сеть** | публичный домен (для HTTPS) | + второй VPS для DR backup |
| **GPU (опц.)** | — | NVIDIA с 4+ GB VRAM для Whisper int8 (faster-whisper) |

## Шаги

### 1. Установите Docker
```bash
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Клонируйте репозиторий
```bash
git clone https://github.com/mocartlex-wq/cognitive-core.git
cd cognitive-core
```

### 3. Сгенерируйте секреты
```bash
bash scripts/gen-secrets.sh > .env
chmod 600 .env
```

Откройте `.env` и заполните **обязательные** поля:
```ini
# Домен
PUBLIC_DOMAIN=cogcore.your-company.ru

# Postgres / Redis / MinIO пароли — сгенерированы в gen-secrets.sh

# LLM (минимум один)
DEEPSEEK_API_KEY=sk-...           # самый дешёвый
# или
OPENAI_API_KEY=sk-...
CLAUDE_API_KEY=sk-ant-...

# Email (для OTP-логина пользователей)
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=465
SMTP_USER=cogcore@your-company.ru
SMTP_PASS=...                     # app-password Yandex

# Per-agent ключи (для bootstrap)
AGENT_API_KEYS=admin:$(openssl rand -hex 32)
```

### 4. Получите TLS-сертификат
```bash
# Let's Encrypt через certbot
sudo apt install certbot
sudo certbot certonly --standalone -d cogcore.your-company.ru
# Скопируйте fullchain.pem + privkey.pem в nginx/certs/
sudo cp /etc/letsencrypt/live/cogcore.your-company.ru/fullchain.pem nginx/certs/server.crt
sudo cp /etc/letsencrypt/live/cogcore.your-company.ru/privkey.pem  nginx/certs/server.key
```

### 5. Запустите стек

**CPU-only установка (без GPU):**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**С GPU (Whisper транскрипция в realtime):**
```bash
# Установите NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/libnvidia-container/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo systemctl restart docker

# Запуск с GPU
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d
```

### 6. Примените миграции
```bash
docker compose exec api alembic upgrade head
```

### 7. Проверьте
```bash
curl https://cogcore.your-company.ru/health
# Ожидается: {"healthy": true, ...}

curl -H "X-API-Key: $(grep AGENT_API_KEYS .env | cut -d: -f2)" \
  https://cogcore.your-company.ru/agents/heartbeat
# Ожидается: 200 OK
```

### 8. Создайте первого пользователя
- Откройте https://cogcore.your-company.ru/ui/login
- Введите email вашего админа → получите OTP по email → войдите
- В профиле — создайте первого агента → получите api_key

## Бэкапы (обязательно)

```bash
# Daily backup на холодный HDD
sudo crontab -e
# Добавьте строку:
0 3 * * * cd /opt/cognitive-core && bash scripts/backup-daily.sh

# Offsite backup (опционально, рекомендация)
# В .env добавьте RCLONE_REMOTE=... (см. scripts/offsite-backup.sh)
```

## Мониторинг (рекомендуется)

```bash
# Prometheus + Grafana stack
cd /opt && git clone https://github.com/mocartlex-wq/cognitive-monitoring.git
cd cognitive-monitoring
docker compose up -d
# Grafana: https://your-domain.ru:3001 (admin / см. .env)
```

5 дашбордов из коробки: API health, Memory layers, LLM usage, System resources, Memory analytics.

## Подключение Claude Code / Cursor / ChatGPT к self-hosted

Точно так же как для облачной версии:
- В Claude Code / Cursor — JSON в `~/.claude.json`:
```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "npx",
      "args": ["mcp-remote", "https://cogcore.your-company.ru/mcp/sse"],
      "env": {"X-API-Key": "ваш-api-key"}
    }
  }
}
```

## Обновление

```bash
cd /opt/cognitive-core
git fetch origin && git checkout v0.7.0  # или нужный tag
docker compose pull
docker compose up -d
docker compose exec api alembic upgrade head
```

Auto-deploy через systemd timer — см. [`AGENT_OPERATIONS.md`](../AGENT_OPERATIONS.md).

## Известные ограничения self-hosted

| Ограничение | Workaround |
|---|---|
| Single-node — нет HA из коробки | Postgres streaming replication + nginx load-balancer (см. [`DEPLOY-SERVER.md`](../DEPLOY-SERVER.md) §HA) |
| Auto-update через сервер mcp.me-ai.ru недоступен | `git pull` вручную или через ваш CI/CD |
| Поддержка по платным каналам отсутствует (Free) | Email community: support@me-ai.ru, response 2-5 рабочих дней |
| Лицензионные ограничения LLM провайдеров | Используйте on-premise: Ollama / vLLM / GigaChat self-hosted (Sber) |

## Enterprise support (платный)

Если нужны:
- 99.95% SLA с фин. ответственностью
- 1-час response для P1 инцидентов
- Quarterly security audit + custom features
- Помощь с миграцией / setup-инженер на месте

→ Свяжитесь sales@me-ai.ru. Pricing — от 50 000 ₽/мес.

## Полезные ссылки

- [SECURITY.md](../SECURITY.md) — threat model + меры защиты
- [DEPLOY-SERVER.md](../DEPLOY-SERVER.md) — детальный гайд деплоя
- [AGENT_OPERATIONS.md](../AGENT_OPERATIONS.md) — runbook для оператора
- [compliance-152fz.md](compliance-152fz.md) — соответствие 152-ФЗ
- [concepts.md](concepts.md) — архитектура памяти

## Поддержка

- Email: support@me-ai.ru
- Issues: https://github.com/mocartlex-wq/cognitive-core/issues
