# Quickstart: AI Video Generation (Kling) за 15 минут

## Что это

`cognitive_video_generate` + `cognitive_video_status` — пара MCP tools для генерации видео через **Kling.ai** (Kuaishou) и **Sora** (OpenAI, when public). Owner mandate 2026-05-26: «нам нужен видео ИИ для блогинга».

**Use cases:**
- Видео для блога / соцсетей из текстового сценария
- Image-to-video анимация статичных постеров
- Быстрые product demo screenrecorder из mockups
- B-roll для launch announcement

## Шаги

### 1. Получить Kling API ключи

1. https://api.kling.ai → Sign up (китайский ID не требуется в 2026)
2. Console → API Keys → Create
3. Скопировать `access_key` + `secret_key` (показываются ОДИН РАЗ)
4. Стоимость: ~$0.10/sec для kling-v1, ~$0.35/sec для kling-v1-pro. 5-sec видео = $0.50-1.75.

### 2. Добавить ключ в Cognitive Core

https://mcp.me-ai.ru/ui/profile → «🤖 Внешние AI-провайдеры»:

| Поле | Значение |
|---|---|
| Provider | `kling_video` |
| API Key | `<access_key>\|<secret_key>` (одной строкой через `\|`) |

Пример: `AKIA42ABC\|SECRET-xyz-789` (одной строкой, без пробелов).

Жмёшь **Save** → **Test** → должен показать «connected».

### 3. Использовать через MCP

```python
# Submit task
task = cognitive_video_generate(
    prompt="Робот идёт по золотому полю на закате, эпическая кинематография",
    duration_sec=5,
    aspect_ratio="16:9",
)
# → {"task_id": "abc-123", "provider_status": "submitted", "model": "kling-v1", "estimated_duration_sec": 60}

# Poll до готовности (каждые 15-30 секунд)
status = cognitive_video_status(task_id=task["task_id"])
# → {"status": "generating", "progress_pct": 45}
# ... через минуту
status = cognitive_video_status(task_id=task["task_id"])
# → {"status": "completed", "video_url": "https://kling.cdn/abc.mp4", "duration_sec": 5}
```

### 4. Image-to-video режим

Анимация существующей картинки:

```python
task = cognitive_video_generate(
    prompt="Камера медленно отдаляется, листья колышутся ветром",
    image_url="https://example.com/static-poster.jpg",
    duration_sec=5,
)
```

### 5. Скачать + сохранить в свою память

```python
# 1. Download готовое видео (URL действителен 24h)
import urllib.request
urllib.request.urlretrieve(status["video_url"], "/tmp/my_video.mp4")

# 2. Save metadata в Cognitive Core L1
cognitive_remember(
    domain="video_blog",
    task="Сгенерировано видео для blog post 'AI memory'",
    result=f"Kling generated 5s video at {status['video_url']}, prompt='{task['prompt']}'",
    lessons="Pro-модель даёт лучшее качество для close-up faces. kling-v1 ok для wide shots.",
)

# 3. Загрузить в свой Telegram-канал / Instagram через bot API
```

## Best practices

1. **Prompt — короткий и кинематографичный** — Kling работает лучше с visual details (lighting, camera angle, mood) чем с abstract concepts.
2. **5 секунд — стандарт** — длиннее = дороже линейно. Для блогинга 5s часто достаточно.
3. **Aspect ratio под платформу** — 16:9 (YouTube/Twitter), 9:16 (TikTok/Stories/Reels), 1:1 (Instagram posts).
4. **`cognitive_remember` после каждого видео** — domain `video_blog` или per-project. Recall потом найдёт «какой prompt давал лучший результат».
5. **Poll не чаще раз в 15 секунд** — Kling сервер сам rate-limit'ит, не вырубят, но это вежливее + расходы на лишние HTTP меньше.

## Что Sora? (опционально)

Sora (OpenAI) сейчас на wait-list. Когда API GA:
- Добавь provider=`sora_video` ключ в `/ui/profile`
- Замени `provider="kling_video"` на `"sora_video"` в вызовах
- Остальное идентично

Пока — Sora MCP tool возвращает понятный «wait-list» message + рекомендует fallback на Kling.

## Cost calculator

| Длительность | Модель | Цена за 1 видео |
|---|---|---|
| 5s | kling-v1 | $0.50 |
| 5s | kling-v1-pro | $1.75 |
| 10s | kling-v1 | $1.00 |
| 10s | kling-v1-pro | $3.50 |

Для блогинга стандарт **5s @ kling-v1**: 100 видео в месяц = $50. Для качественных — pro $175.

## Архитектура

```
[Agent (Claude Code)] 
   │ MCP cognitive_video_generate(prompt=..., duration=5)
   ↓
[cognitive_api FastAPI :8000]
   │ POST /api/video/generate
   │ - Load Kling key from user_external_keys (Fernet-decrypted)
   │ - app/services/video_providers/kling.py
   │   - Generate JWT (HS256, access_key + secret_key)
   │   - POST https://api.kling.ai/v1/videos/text2video
   ↓
[Kling cloud — 30-180s generation]
   ↓
[Agent polls cognitive_video_status(task_id)]
   │ GET /api/video/status/{task_id}?provider=kling_video  
   ↓
[Kling cloud GET /v1/videos/text2video/{task_id}]
   │ Returns {task_status, task_result.videos[0].url}
   ↓
[Agent downloads video_url, saves to L1 via cognitive_remember]
```

## Troubleshooting

| Симптом | Причина | Fix |
|---|---|---|
| `400 Ключ kling_video не настроен` | provider key не добавлен | /ui/profile → External providers → add |
| `502 Provider error: invalid key format` | передан single key вместо `access\|secret` | Перепроверить формат — через `\|` |
| `task_status=failed` через минуту | content policy violation (NSFW, copyright) | Изменить prompt, убрать persons/brands |
| polling timeout 30s | Kling сервер занят | Подождать 1-2 мин, retry — task всё ещё processing |

## Поддержка
- Email: support@me-ai.ru
- Kling API docs: https://docs.kling.ai
- E2E test scaffold: `tests/test_video_providers.py` (HTTP-mocked, без real key)
