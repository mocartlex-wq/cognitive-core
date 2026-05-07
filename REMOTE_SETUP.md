# Remote Linux Server — Подключение и развёртывание

> Этот гайд: как подключить Linux-сервер к Windows-ПК и развернуть Cognitive Core удалённо.

## Топология подключения

### Вариант A — Прямой Ethernet кабель (рекомендую если оба рядом)

```
[Windows PC]                              [Linux Server]
192.168.50.1/24  ◄── Ethernet cable ──►  192.168.50.2/24
```

**Pros:** 1 Gbps full-duplex, изоляция от роутера, низкая latency
**Cons:** Требует статической настройки IP с обеих сторон

### Вариант B — Через WiFi/роутер (проще)

```
              [Router 192.168.0.1]
             /                     \
       [Windows]                  [Linux Server]
   192.168.0.109                    192.168.0.X (DHCP)
```

**Pros:** Zero-config, оба устройства уже видят друг друга
**Cons:** Скорость WiFi ниже, общий broadcast domain

---

## Что мне нужно от вас

Минимум для удалённой настройки:

| # | Что | Как узнать |
|---|---|---|
| 1 | **IP адрес сервера** | На Linux: `ip a` (искать inet 192.168.x.x), или из роутера |
| 2 | **SSH user + password ИЛИ путь к SSH-ключу** | Стандартный пользователь которого создавали при установке Linux |
| 3 | **Дистрибутив и версия** | `lsb_release -a` или `cat /etc/os-release` |
| 4 | **Включён ли SSH-сервер** | `sudo systemctl status ssh` (Ubuntu) или `sudo systemctl status sshd` |

### Если SSH ещё не включён на сервере

На Linux-сервере выполнить **один раз** (физически перед сервером, или через монитор+клаву):

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable --now ssh
sudo ufw allow 22/tcp 2>/dev/null || true
ip a | grep 'inet 192'        # узнать IP
```

---

## Сценарий 1: Прямой кабель (статический IP)

### На Linux-сервере

```bash
# Узнать имя интерфейса (например enp1s0 или eth0)
ip link show

# Назначить статический IP временно (пропадёт после reboot)
sudo ip addr add 192.168.50.2/24 dev enp1s0
sudo ip link set enp1s0 up

# Сохранить навсегда (Ubuntu 22+):
sudo tee /etc/netplan/99-direct.yaml <<EOF
network:
  version: 2
  ethernets:
    enp1s0:
      addresses: [192.168.50.2/24]
EOF
sudo netplan apply
```

### На Windows PC

```powershell
# В PowerShell от Administrator
# Найти имя Ethernet адаптера (например "Ethernet")
Get-NetAdapter | Where-Object Status -eq 'Up'

# Назначить IP
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.50.1 -PrefixLength 24

# Проверить связь
Test-Connection 192.168.50.2
```

---

## Сценарий 2: Через WiFi/роутер

Ничего настраивать не нужно — IP уже выдан роутером. Просто узнать его на Linux:

```bash
ip a | grep 'inet 192'
# inet 192.168.0.X/24 brd ...
```

---

## Удалённое развёртывание Cognitive Core

После того как SSH работает — запускаю **на этом Windows-ПК**:

```bash
cd "D:/ИИ/память/память 1/cognitive-core"

# Через ключ
SERVER_HOST=192.168.0.100 \
SERVER_USER=admin \
SERVER_KEY=~/.ssh/id_rsa \
bash scripts/bootstrap-remote.sh

# Или через пароль (нужен sshpass — поставится в WSL)
SERVER_HOST=192.168.0.100 \
SERVER_USER=admin \
SERVER_PASS='your-password' \
bash scripts/bootstrap-remote.sh
```

Скрипт делает **7 шагов** автоматически:

| # | Что |
|---|---|
| 1 | Проверка SSH connectivity + диагностика (RAM, CPU, disk) |
| 2 | Установка Docker + Compose plugin (если нет) |
| 3 | Подготовка `/opt/cognitive-core` с правильными правами |
| 4 | Перенос проекта через rsync (или tar если rsync нет) |
| 5 | Запуск `install-server.sh` (gen-secrets, TLS, UFW, compose up) |
| 6 | Wait for healthy (30 попыток × 5 сек) |
| 7 | Финальный отчёт + endpoints |

---

## Подключение клиентов после развёртывания

### Claude Desktop / Cherry Studio с Windows-ПК → Linux-сервер

В `claude_desktop_config.json` (или Cherry Studio MCP):

```json
{
  "mcpServers": {
    "cognitive-core-lan": {
      "command": "mcp-proxy",
      "args": ["--transport", "sse", "http://192.168.0.100:9001/mcp/sse"],
      "env": { "X-API-Key": "<agent-key>" }
    }
  }
}
```

API key возьмёт скрипт после установки и положит в `/opt/cognitive-core/.env` на сервере, либо распечатает в финальном отчёте.

### Direct REST API

```bash
# С Windows-ПК
curl -H "X-API-Key: $KEY" http://192.168.0.100:9001/health
```

---

## Troubleshooting

| Проблема | Решение |
|---|---|
| `ssh: connect to host ... port 22: Connection refused` | SSH не запущен → `sudo systemctl start ssh` на сервере |
| `permission denied (publickey)` | Скопировать ключ: `ssh-copy-id user@host` |
| `sudo: a password is required` | Дать NOPASSWD: `echo "$USER ALL=(ALL) NOPASSWD: ALL" \| sudo tee /etc/sudoers.d/$USER` |
| Docker install падает на Debian | Использовать `apt install docker.io docker-compose-plugin` вместо get.docker.com |
| Cannot reach 9001 from Windows | UFW: `sudo ufw allow 9001/tcp` или открыть в роутере |
| Slow rsync transfer | Использовать прямой кабель (Gigabit) вместо WiFi |

---

## Что я могу сделать после получения SSH-доступа

Когда дадите SERVER_HOST + USER + KEY/PASSWORD, я выполню:

1. ✅ Подключусь по SSH, проверю железо (CPU/RAM/диск)
2. ✅ Идентифицирую дистрибутив, выберу правильный install path
3. ✅ Установлю Docker если нет
4. ✅ Перенесу проект с этого ПК на сервер через rsync
5. ✅ Запущу `install-server.sh` (gen-secrets, TLS, firewall, compose up)
6. ✅ Дождусь health и проверю работу всех 5 слоёв
7. ✅ Настрою UFW: открою 22, 80, 443, 9001 — закрою остальное
8. ✅ Проброшу backup-cron на хост
9. ✅ Дам вам endpoints + agent API keys + JSON конфиг для Claude Desktop
10. ✅ Если позже понадобится — добавлю GPU stack когда RTX появится
