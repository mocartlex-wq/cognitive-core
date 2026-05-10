# Cognitive Core — 5-min screencast script

**Goal**: in 5 minutes, demo enough to convince a developer to `git clone` & try.
**Audience**: HN/Reddit/Habr crowd. Sceptical of yet-another-agent-framework.
**Style**: terminal + small browser inset. No talking-head. Voice-over.

## Beat sheet (300 s budget)

| Time | Beat | What's on screen | Voice-over (EN) |
|------|------|------------------|-----------------|
| 0:00 – 0:15 | Hook | Title card → clip of Claude Code & ChatGPT side-by-side, both joining the same room | "What if Claude and ChatGPT could collaborate in the same room? Right now, they can't talk to each other. Let's fix that — in five minutes." |
| 0:15 – 0:45 | Install | Terminal: `curl -fsSL .../quickstart.sh \| bash` → containers spin up | "One curl. One docker compose. Postgres, Redis, MinIO, NATS, the API and a rooms service — all set up with random secrets. No login, no SaaS." |
| 0:45 – 1:15 | First room | `make smoke` runs → green checks → open browser to /ui | "Smoke test passes. We've got a working stack. Let's create our first room." |
| 1:15 – 2:00 | Two agents | Alice (Claude Code, left pane) creates `design-review` room. Bob (ChatGPT, right pane) joins via REST | "Alice runs Claude Code with our MCP wrapper. Bob is plain-vanilla ChatGPT hitting the REST API. Same room. Same key. Different platforms." |
| 2:00 – 2:50 | Long-poll ask | Alice asks "review PR #42". Connection hangs. Bob types reply. Alice's terminal unblocks instantly | "Alice asks. The connection holds open — no polling, no webhooks. Bob types. Alice's terminal returns the answer immediately. Latency: under 800 milliseconds." |
| 2:50 – 3:50 | Offline fallback | Bob's pane goes dark (laptop closes). Alice asks again. After 5s a `[proxy-tentative]` answer from DeepSeek arrives. Bob wakes, runs `cognitive_room_sync_pending`, sees proxy answer, posts override | "Now Bob's laptop is asleep. Alice still asks. After five seconds the server generates a tentative DeepSeek answer — marked clearly as a proxy. When Bob wakes up, he sees the question and the proxy reply. He overrides with a real answer in one tool call." |
| 3:50 – 4:20 | Memory | Quick cut to L1 events table → L3 master_knowledge table → cogcore-search showing the conversation later | "Everything is logged in the five-layer memory. Tomorrow Alice can recall what was discussed — searchable, persistent, on her own server." |
| 4:20 – 4:50 | Why this is different | Comparison card: vs LangChain, AutoGen, Assistants | "LangChain is Python only. AutoGen is Python only. OpenAI Assistants locks you in. Cognitive Core is REST. MIT-licensed. Self-hosted. Run it in your homelab tonight." |
| 4:50 – 5:00 | CTA | github URL → discord URL → "Star · Try · Tell us what's broken" | "Repo's in the description. Star it, try it, tell us what's broken. Five minutes to a multi-agent room. Let's go." |

## Hot-key cheat sheet for the recording

| Step | Command | Pre-condition |
|------|---------|---------------|
| Install | `bash quickstart.sh` | empty box, docker installed |
| Smoke   | `make smoke` | stack up |
| Open UI | `xdg-open http://localhost:9098/ui` | browser ready |
| Create  | `curl -X POST localhost:9098/rooms -d '{"name":"design-review","creator":"alice"}'` | jq optional |
| Join Bob (UI) | navigate to `/ui` → enter ROOM_ID + KEY → "Join" | |
| Ask     | see `screencast/scenes/03_ask.sh` |
| Bob offline | `pkill -STOP -f cognitive_core_mcp` (suspends) | |
| Sync-pending | `curl ".../sync-pending?agent_id=bob" -H "X-Room-Key: $KEY"` | |
| Memory pic | `psql -c 'SELECT count(*) FROM l1_raw_events; SELECT count(*) FROM l3_master_knowledge;'` | |

## Voice-over guidelines

- Pace: ~150 words/min English; ~135 wpm Russian (slightly slower).
- Tone: confident, slightly under-stated. No hype words ("revolutionary", "magical").
- No live audio of typing — overlay key clicks in post.
- One 0.5-s pause at every beat boundary so subtitles can breathe.

## Aspect / quality

- 1920×1080, 30 fps.
- Terminal: 14 px monospace, dark theme (`Solarized Dark` or default Ghostty).
- Browser: 1280×720 inset, top-right.
- Captions burned-in for the intro hook only; full SRTs side-loaded.
- File: `cogcore-demo-v1.mp4`, H.264 yuv420p, ≤ 50 MB → fits HN attachment.

## What NOT to show

- L4/L5 internals (too technical for the 5 min budget).
- Approval flow / lock manager / KG (these go into the deep-dive video later).
- Anything failing live — record clean takes; bloopers go in a separate "behind
  the scenes" cut for engagement after launch.
