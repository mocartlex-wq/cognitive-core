# Quickstart: Telegram-бот с памятью за 15 минут

## Что получится

Telegram-бот на `aiogram` (или `python-telegram-bot`), который **помнит каждого пользователя**: историю переписки, контекст, факты — между сессиями, между рестартами бота, между нагрузкой кластера.

Память изолируется per-user (Telegram `user_id` → `domain`), бот сам не путает разных людей.

## Сценарий применения

- Личный AI-ассистент в TG (как ChatGPT, но с памятью)
- Customer support бот, помнящий историю клиента
- Game-bot со state'ом игры между турнами
- Любой бот, где нужен «помнить что было до этого»

## Шаги

### 1. Создайте Telegram-бота
- Откройте https://t.me/BotFather → `/newbot` → имя → @username → получите **`TG_TOKEN`**

### 2. Зарегистрируйтесь в Cognitive Core
- https://mcp.me-ai.ru/ui/pricing → «Начать бесплатно» → email → OTP
- Профиль → «Мои помощники» → создайте `tg-bot` → скопируйте **`COG_API_KEY`**

### 3. Установите зависимости
```bash
pip install aiogram httpx
```

### 4. Минимальный код

```python
# bot.py
import asyncio
import os
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart

TG_TOKEN = os.environ["TG_TOKEN"]
COG_API_KEY = os.environ["COG_API_KEY"]
COG_BASE = "https://mcp.me-ai.ru"

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()


async def cog_remember(user_id: int, message: str, response: str = "") -> None:
    """Сохраняет реплику пользователя + ответ бота в персональный domain."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{COG_BASE}/events",
            headers={"X-API-Key": COG_API_KEY},
            json={
                "domain": f"tg_user_{user_id}",
                "payload": {"user_text": message, "bot_text": response},
            },
        )


async def cog_recall(user_id: int, query: str, limit: int = 5) -> list[dict]:
    """KNN-поиск по истории конкретного пользователя."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{COG_BASE}/mcp/messages",
            headers={"X-API-Key": COG_API_KEY},
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "cognitive_recall",
                    "arguments": {
                        "query": query,
                        "domain": f"tg_user_{user_id}",
                        "limit": limit,
                    },
                },
            },
        )
    return r.json().get("result", {}).get("structuredContent", {}).get("hits", [])


@dp.message(CommandStart())
async def start(msg: types.Message) -> None:
    await msg.answer("Привет! Я бот с памятью. Расскажи что-нибудь — я запомню.")


@dp.message(F.text)
async def chat(msg: types.Message) -> None:
    user_id = msg.from_user.id
    text = msg.text

    # Recall context перед ответом
    hits = await cog_recall(user_id, text, limit=3)
    context = "\n".join(
        f"- было: {h['payload'].get('user_text', '')}"
        for h in hits
    ) or "(история пуста)"

    # Здесь — вставьте свой LLM call (OpenAI/Claude/DeepSeek/etc)
    # Пример простой эхо-логики с использованием контекста:
    reply = f"Вы написали: «{text}»\n\nИз вашей памяти:\n{context}"

    await msg.answer(reply)

    # Save после ответа
    await cog_remember(user_id, text, reply)


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
```

### 5. Запустите

```bash
export TG_TOKEN="123456:ABC..."
export COG_API_KEY="cogc_xxxxx..."
python bot.py
```

### 6. Тестируйте
- Напишите боту что-нибудь — бот ответит и запомнит
- Закройте/перезапустите процесс
- Напишите боту что-то близкое к первому сообщению — бот «вспомнит» благодаря KNN-recall

## Production-чеклист

| Пункт | Зачем | Как сделать |
|---|---|---|
| **Webhook вместо polling** | Скейл, латенси | aiogram supports webhook. Хост на mcp.me-ai.ru/your-bot через nginx-reverse |
| **PII-cleanup** | 152-ФЗ | Не сохраняйте номера телефонов, email напрямую — только references на ваш external user DB |
| **Rate-limit per user** | DoS защита | Простой Redis-counter `tg_user_X_rps` |
| **LLM-провайдер** | Качество ответа | Подставьте DeepSeek (дёшево, рус.), GigaChat (РФ), Claude (best quality) |
| **Multi-language** | UX | Cognitive Core хранит UTF-8, нативно работает с любым языком |
| **Backup истории** | DR | `/user/data-export` раз в неделю → ваш S3 |

## Удаление по запросу пользователя

```python
@dp.message(Command("forget_me"))
async def forget(msg: types.Message) -> None:
    user_id = msg.from_user.id
    # Удаляет все события с domain=tg_user_X
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(
            f"{COG_BASE}/admin/events",
            headers={"X-API-Key": COG_API_KEY},
            params={"domain": f"tg_user_{user_id}"},
        )
    await msg.answer("Ваша история стёрта.")
```

## Quota и масштаб

- **Free**: 10 000 событий/день — ~5000 турнов/день
- **Pro (490₽/мес)**: 100 000/день — ~50 000 турнов
- **Enterprise (по запросу)**: без жёстких лимитов

При нагрузке 1+ млн запросов/день — нужна Enterprise + dedicated instance.

## Поддержка

- Email: support@me-ai.ru
- Документация: https://mcp.me-ai.ru/docs/concepts.md
- Telegram-канал платформы: будет создан после launch — следите в README
