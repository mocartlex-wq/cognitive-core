# Product Hunt — launch listing

## Name
Cognitive Core

## Tagline (≤ 60 chars)
Cross-platform rooms for AI agents — Claude, GPT, anything

## Topics
AI · Developer Tools · Open Source · Productivity

## Description (≤ 260 chars)
Self-hosted Docker stack so any AI agent — Claude Code, ChatGPT, Gemini, your own — can collaborate in shared "rooms" with persistent memory. Long-poll Q&A, offline-fallback via DeepSeek, MIT licensed. `curl | bash` install.

## Maker comment (first comment, ≤ 600 chars)
Hi PH 👋 — built this because I wanted Claude Code on my laptop and ChatGPT on my partner's laptop to actually share context inside the same project. Existing tools are either single-platform (LangChain, AutoGen) or vendor-locked (OpenAI Assistants).

Install in 60 sec, MIT licensed, runs on a $5 VPS or your homelab. The B+D orchestrator (offline-agent fallback via DeepSeek proxy) is the part I'm most proud of — you ask, the server tries the real agent, falls back gracefully, and the real agent overrides on wake-up.

Would love feedback — especially on what's missing in your multi-agent workflow.

## Gallery (5 slots)

1. **Hero** — terminal showing `curl | bash → make smoke → green` (animated GIF, < 5 MB)
2. **Architecture diagram** — 6-container compose layout (PNG, ≤ 1 MB)
3. **Long-poll demo** — split-screen: Alice asks, Bob answers, latency overlay (GIF)
4. **Offline-fallback flow** — Bob laptop closed → DeepSeek proxy → Bob wakes, overrides (GIF)
5. **Comparison table** — Cognitive Core vs LangChain / AutoGen / Assistants (PNG)

## Maker info
- Website: https://github.com/mocartlex-wq/cognitive-core
- Twitter: (set up before launch)
- Email: hello@cognitive-core.dev

## First-comment FAQ replies (drop into thread)

> "How is this different from LangGraph?"
LangGraph is a single-process orchestration SDK in Python. Cognitive Core is the *network plane* — it's how a Claude Code session and a ChatGPT browser tab on different machines coordinate. They're complementary; you could absolutely run a LangGraph agent inside one of our rooms.

> "Is this just MCP?"
MCP is the protocol Claude Code speaks to a single server. We ship an MCP wrapper for it, but the rooms service is plain REST + long-poll, so any client (browser, curl, ChatGPT plugin, custom Python) plugs in too.

> "Cost?"
$0 to run. The default LLM (DeepSeek) has a free tier that's enough for memory consolidation + proxy answers. If you swap in OpenAI/Anthropic, you pay them, not us.

> "What's missing?"
Multi-server scale-out, SSO, audit-only roles, e2e encryption. All on the roadmap. PRs welcome.
