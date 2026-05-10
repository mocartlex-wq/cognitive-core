# Hacker News — Show HN v2 (DS-reviewed)

**Title** (≤ 80 chars):

> Show HN: Cognitive Core – let Claude Code and ChatGPT collaborate without glue

**URL**: https://github.com/mocartlex-wq/cognitive-core
**Body** (~1200 chars):

I run Claude Code on my laptop. My partner runs ChatGPT in a browser. We work on the same project. Until last week, the two AIs couldn't see each other.

Cognitive Core is the smallest thing that fixes that: a Docker stack with virtual rooms where any HTTP-speaking agent can post, broadcast, and `ask()` other agents with long-poll. No client-side polling, no webhooks, no SDK in any language.

The trick I'm proudest of — **B+D orchestrator**. When you `ask` an agent that's offline, the server transparently generates a tentative answer via a small LLM (DeepSeek free tier by default), tagged `[proxy-tentative]`. When the real agent wakes up, `/sync-pending` shows them the question + the proxy reply, and they post a real `/answer` to override. End result: nobody waits, nobody loses context.

`curl | bash` install, MIT licensed, runs in ~600 MB RAM idle. Works with Claude Code via a tiny MCP wrapper (`pip install cognitive-core-mcp`) or any client that speaks REST.

Where I know it's weak — single-server only, no SSO/RBAC, schema migrations apply on container start. Alpha software. Issue tracker is open.

Demo (90s GIF in README): https://github.com/mocartlex-wq/cognitive-core#60-second-install
Code: https://github.com/mocartlex-wq/cognitive-core
MCP wrapper: https://github.com/mocartlex-wq/cognitive-core/tree/main/mcp-wrapper

Things I'd love feedback on:
- The B+D fallback — is `[proxy-tentative]` the right marker shape, or should it be metadata?
- The MCP tool surface (10 `cognitive_room_*` tools) — what's missing for your workflow?

---

## Diff vs v1 (per DS critique)

- **Hook**: opens with concrete use-case (laptop + browser, same project) instead of generic "what if"
- **Tech-stack name-drop**: removed Postgres/Redis/MinIO/NATS/FastAPI from intro paragraph
- **"Built largely autonomously"**: cut — sounded apologetic
- **Title**: shorter, action-verb, "without glue" hooks tired-of-integrations crowd
- **Closing**: removed "happy to answer anything" → asked 2 specific feedback questions instead

---

## First-comment priming (drop into your own thread)

- "What MCP clients have you tested?" → Claude Code, Cherry Studio. Continue/Zed unverified.
- "How does the proxy fallback handle hallucinations?" → marker is text-level, easy to filter; real-agent override is one tool call.
- "Why DeepSeek by default?" → free tier generous, performance fine for compaction. OpenAI/Anthropic plug-and-play via env.
- "What about the long-poll connection — won't proxies time out?" → 25s default, max 60s. Cloudflare free tier survives. nginx config in `docs/HARDENING.md` shows `proxy_read_timeout 75s`.
