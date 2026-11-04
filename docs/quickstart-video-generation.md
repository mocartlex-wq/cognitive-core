# Quickstart: AI video generation (Kling) in 15 minutes

> 🇷🇺 Русская версия: [quickstart-video-generation.ru.md](quickstart-video-generation.ru.md)

## What it is

`cognitive_video_generate` + `cognitive_video_status` are a pair of MCP tools for generating video through **Kling.ai** (Kuaishou) and **Sora** (OpenAI, once public). The owner's mandate from 2026-05-26: "we need a video AI for blogging".

**Use cases:**
- Blog and social-media videos from a text script
- Image-to-video animation of static posters
- Quick product demos from mockups
- B-roll for a launch announcement

## Steps

### 1. Get Kling API keys

1. https://api.kling.ai → Sign up (no Chinese ID required as of 2026)
2. Console → API Keys → Create
3. Copy the `access_key` + `secret_key` (shown ONCE)
4. Cost: ~$0.10/sec for kling-v1, ~$0.35/sec for kling-v1-pro. A 5-second video is $0.50–1.75.

### 2. Add the key to Cognitive Core

https://mcp.me-ai.ru/ui/profile → "🤖 External AI providers":

| Field | Value |
|---|---|
| Provider | `kling_video` |
| API Key | `<access_key>\|<secret_key>` (one string joined with `\|`) |

Example: `AKIA42ABC\|SECRET-xyz-789` (one string, no spaces).

Click **Save** → **Test**; it should report "connected".

### 3. Use it over MCP

```python
# Submit a task
task = cognitive_video_generate(
    prompt="A robot walks through a golden field at sunset, epic cinematography",
    duration_sec=5,
    aspect_ratio="16:9",
)
# → {"task_id": "abc-123", "provider_status": "submitted", "model": "kling-v1", "estimated_duration_sec": 60}

# Poll until ready (every 15–30 seconds)
status = cognitive_video_status(task_id=task["task_id"])
# → {"status": "generating", "progress_pct": 45}
# ... a minute later
status = cognitive_video_status(task_id=task["task_id"])
# → {"status": "completed", "video_url": "https://kling.cdn/abc.mp4", "duration_sec": 5}
```

### 4. Image-to-video mode

Animate an existing picture:

```python
task = cognitive_video_generate(
    prompt="The camera slowly pulls back, leaves sway in the wind",
    image_url="https://example.com/static-poster.jpg",
    duration_sec=5,
)
```

### 5. Download and save to your memory

```python
# 1. Download the finished video (the URL is valid for 24h)
import urllib.request
urllib.request.urlretrieve(status["video_url"], "/tmp/my_video.mp4")

# 2. Save metadata to Cognitive Core L1
cognitive_remember(
    domain="video_blog",
    task="Generated a video for the blog post 'AI memory'",
    result=f"Kling generated 5s video at {status['video_url']}, prompt='{task['prompt']}'",
    lessons="The pro model gives better quality for close-up faces. kling-v1 is fine for wide shots.",
)

# 3. Publish to your Telegram channel / Instagram through the bot API
```

## Best practices

1. **Keep the prompt short and cinematic.** Kling works better with visual details (lighting, camera angle, mood) than with abstract concepts.
2. **5 seconds is the standard.** Longer costs linearly more. For blogging, 5 s is usually enough.
3. **Match the aspect ratio to the platform**: 16:9 (YouTube/Twitter), 9:16 (TikTok/Stories/Reels), 1:1 (Instagram posts).
4. **`cognitive_remember` after every video**, in the `video_blog` domain or per project. Recall will later find "which prompt gave the best result".
5. **Poll no more often than every 15 seconds.** Kling rate-limits on its side; you won't be cut off, but it is politer and saves HTTP overhead.

## What about Sora? (optional)

Sora (OpenAI) is currently wait-listed. Once the API is generally available:
- Add a `sora_video` provider key in `/ui/profile`
- Replace `provider="kling_video"` with `"sora_video"` in your calls
- Everything else is identical

Until then the Sora MCP tool returns a clear "wait-list" message and recommends falling back to Kling.

## Cost calculator

| Duration | Model | Price per video |
|---|---|---|
| 5 s | kling-v1 | $0.50 |
| 5 s | kling-v1-pro | $1.75 |
| 10 s | kling-v1 | $1.00 |
| 10 s | kling-v1-pro | $3.50 |

For blogging the standard is **5 s @ kling-v1**: 100 videos a month = $50. For higher quality, pro = $175.

## Architecture

```
[Agent (Claude Code)]
   │ MCP cognitive_video_generate(prompt=..., duration=5)
   ↓
[cognitive_api FastAPI :8000]
   │ POST /api/video/generate
   │ - Load the Kling key from user_external_keys (Fernet-decrypted)
   │ - app/services/video_providers/kling.py
   │   - Generate a JWT (HS256, access_key + secret_key)
   │   - POST https://api.kling.ai/v1/videos/text2video
   ↓
[Kling cloud — 30–180 s generation]
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

| Symptom | Cause | Fix |
|---|---|---|
| `400 kling_video key is not configured` | provider key not added | /ui/profile → External providers → add |
| `502 Provider error: invalid key format` | a single key was passed instead of `access\|secret` | Re-check the format: joined with `\|` |
| `task_status=failed` after a minute | content policy violation (NSFW, copyright) | Change the prompt; remove persons/brands |
| polling timeout 30 s | the Kling server is busy | Wait 1–2 min and retry; the task is still processing |

## Support
- Email: support@me-ai.ru
- Kling API docs: https://docs.kling.ai
- E2E test scaffold: `tests/test_video_providers.py` (HTTP-mocked, no real key)
