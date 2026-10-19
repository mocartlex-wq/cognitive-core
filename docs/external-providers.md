# External AI providers (per-tenant keys)

> 🇷🇺 Русская версия: [external-providers.ru.md](external-providers.ru.md)

Cognitive Core supports **opt-in** use of your own API keys for external AI providers in the vision analysis of videos. The platform uses **your** key, and the charges land on **your** account with the provider.

## Why

By default the platform uses:
1. Shared Qwen-VL (Alibaba), paid for by the platform
2. DeepSeek text-only fallback if Qwen is unavailable

If you need better or more specialised vision processing (for example MiniMax / Claude for particular tasks, or GigaChat for compliance), add your own key and `vision_analyzer` will automatically try it first.

## Where to configure

1. Sign in at [https://mcp.me-ai.ru/ui/profile](https://mcp.me-ai.ru/ui/profile)
2. Open the **"🤖 External AI providers"** card
3. Expand the provider you need → enter the key → **Save** → **Test**

## Provider preference order

If you have several keys configured, `vision_analyzer` tries them in this order (first success wins):

| # | Provider | Label | Where to get a key | Cost per 12-frame video |
|---|----------|-------|--------------------|-------------------------|
| 1 | `qwen` | Qwen-VL (Alibaba) | [DashScope console](https://dashscope.console.aliyuncs.com/apiKey) | ~$0.02–0.03 |
| 2 | `minimax` | MiniMax (Hailuo) | [MiniMax platform](https://www.minimax.io/platform/keys) | ~$0.04–0.08 |
| 3 | `gigachat` | GigaChat (Sber) | [Sber Developers](https://developers.sber.ru/portal/products/gigachat-api) | ~10 ₽ |
| 4 | `claude` | Claude Haiku (Anthropic) | [Anthropic Console](https://console.anthropic.com/settings/keys) | ~$0.005–0.015 |
| 5 | `openai` | GPT-4o-mini (OpenAI) | [OpenAI Platform](https://platform.openai.com/api-keys) | ~$0.01–0.02 |
| 6 | `gemini` | Gemini 2.0 Flash (Google) | [Google AI Studio](https://ai.google.dev/) | ~$0.001–0.005 |

If all your keys fail (auth_failed / rate_limit / network), the fallback transparently goes to shared Qwen + DeepSeek, as before.

## What quality to expect

- **Qwen-VL** — best for Russian transcript context, describes user interfaces well
- **MiniMax** — visual reasoning (what exactly is on the frames, analysis of deltas)
- **Claude Haiku** — stable, short, precise
- **GPT-4o-mini** — price/quality balance for varied videos
- **Gemini 2.0 Flash** — fastest (typically 1–3 s), good for short clips
- **GigaChat** — hosted in Russia (for compliance-sensitive tenants)

## Security

- **Encrypted at rest:** keys are encrypted with Fernet (symmetric AES-128 + HMAC) before being stored in the database. Plaintext is never stored.
- **Never shown in the UI:** only the first 4 and last 4 characters are displayed (`sk-x...XYZ7`), even in the /admin interface.
- **Never logged:** plaintext keys never reach the logs (not even in DEBUG mode or exception messages).
- **Audit trail:** every use of your key is written to L1 (`domain=external_key_usage`), including provider / model / tokens. Available through `cognitive_recall(domain='external_key_usage')`.

## Overriding base_url / model_name

Expand the "Advanced" section in the UI when saving; you can set:

- **base_url** — for example `https://api.openai.com/v1` (or your proxy)
- **model_name** — for example `claude-sonnet-4-5` instead of `claude-haiku-4-5`

This lets you:
- Use regional endpoints (EU / US / CN)
- Pick a specific model (cheap Haiku vs strong Opus)
- Route through corporate proxies or OpenAI-compatible self-hosted servers (Ollama, LM Studio)

## API endpoints

If you are writing your own client:

```http
GET    /user/settings/external-keys
       → { items: [{provider, masked_key, last_test_status, ...}] }

POST   /user/settings/external-key
       Body: { provider, api_key, base_url?, model_name? }
       → { ok, masked_key }

POST   /user/settings/external-key/{provider}/test
       → { ok, message, latency_ms, status }

DELETE /user/settings/external-key/{provider}
       → { ok }
```

All of them require a valid session cookie (sign in via one-time code at /ui/login).

## FAQ

**Q: The test shows auth_failed. What do I do?**
A: The key is invalid or expired. Create a new one on the provider's portal and update it through /ui/profile (UPSERT: the old value is overwritten).

**Q: How many keys can I store?**
A: One per provider (PRIMARY KEY = owner_user_id + provider). At most 6 in total (Qwen, MiniMax, GigaChat, Claude, OpenAI, Gemini).

**Q: What happens to my keys if my account is deleted?**
A: CASCADE: all keys are deleted automatically with the account.

**Q: Where can I see how much I spent on vision?**
A: `cognitive_recall(domain='external_key_usage', limit=50)` over MCP shows every call with tokens_in / tokens_out / provider.

**Q: What if I have no keys at all?**
A: The vision analyzer falls back to shared Qwen (if the platform admin configured it) or DeepSeek text-only. No errors; everything keeps working.

## What the platform does NOT do

- It does NOT create shared platform keys for MiniMax / Claude / OpenAI / Gemini; those are opt-in per tenant only. Only Qwen has a shared fallback (paid by the platform owner).
- It does NOT integrate with Stripe / billing for this spend; you pay the provider directly.
- It does NOT cache responses between tenants; every request goes through your key.

---

## Video generation providers (post-launch, 2026-05-26)

A separate channel from the vision providers: the `cognitive_video_generate` MCP tool. Details: [quickstart-video-generation.md](quickstart-video-generation.md).

| Provider | Region | Price (5 s video) | Quality | Where to get a key |
|---|---|---|---|---|
| `kling_video` (Kuaishou) | China, reachable from Russia | $0.50 (v1) / $1.75 (pro) | Best mainstream option in 2026 | https://api.kling.ai |
| `sora_video` (OpenAI) | USA, VPN needed from Russia | TBA | Preview, wait-list | https://openai.com/sora |

### Key format

| Provider | Field in /ui/profile | Example |
|---|---|---|
| `kling_video` | `access_key\|secret_key` (single string joined with `\|`) | `AKIA42\|SECRET-xyz` |
| `sora_video` | `sk-...` (OpenAI standard) | `sk-proj-abc123` (once GA) |

### Use case

The owner's mandate from 2026-05-26: "we need a video AI for creating video clips and processing video, for blogging". We start with Kling: it has a public API and works from Russia. Sora will follow when OpenAI opens it up.

Cost for blogging: 100 videos of 5 s @ kling-v1 = $50/month. Acceptable for a professional blogger.
