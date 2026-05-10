#!/usr/bin/env python3
"""Patch cognitive-rooms.py to add AI assistant routes."""
import sys
import os
import time

p = sys.argv[1] if len(sys.argv) > 1 else "/opt/cogcore-demo/extras/cognitive-rooms.py"
src = open(p).read()

if "_ui_assistant_page" in src:
    print("already patched, skipping")
    sys.exit(0)

# ===== Helpers block (inserted before the request handler class) =====
helpers = r'''

# -------------------------------------------------------------------
# AI Assistant - onboarding helper backed by DeepSeek
# -------------------------------------------------------------------
import urllib.request as _ureq
import urllib.error as _uerr

_DOCS_CACHE = None
_DOCS_CACHE_TS = 0


def _load_docs_context(max_chars=8000):
    global _DOCS_CACHE, _DOCS_CACHE_TS
    if _DOCS_CACHE and (time.time() - _DOCS_CACHE_TS) < 600:
        return _DOCS_CACHE
    paths = [
        "/app/extras/README.md",
        "/app/extras/docs/ROOMS.md",
        "/app/extras/docs/MCP.md",
        "/app/extras/docs/MEMORY.md",
        "/app/extras/docs/architecture.md",
        "/app/extras/docs/HARDENING.md",
    ]
    chunks = []
    for fp in paths:
        try:
            with open(fp) as f:
                txt = f.read()[:1500]
            chunks.append("### " + os.path.basename(fp) + "\n" + txt)
        except Exception:
            pass
    ctx = "\n\n".join(chunks)[:max_chars]
    _DOCS_CACHE = ctx
    _DOCS_CACHE_TS = time.time()
    return ctx


def _assistant_system_prompt():
    return (
        "You are an AI helper for the Cognitive Core project. Explain functionality "
        "in simple terms for non-developers. Reply in Russian by default (or in the "
        "user's language). Be concrete: provide shell commands, doc links, examples. "
        "If you don't know - say so honestly. Don't hallucinate. Keep replies under "
        "300 words. For deployment questions - give concrete steps. For architecture - "
        "use everyday analogies.\n\n"
        "Project documentation context:\n\n"
        + _load_docs_context()
    )


def _call_deepseek_chat(user_msg, history=None):
    api_key = get_deepseek_key()
    if not api_key:
        return None, "DEEPSEEK_API_KEY not set on server. Assistant unavailable."
    messages = [{"role": "system", "content": _assistant_system_prompt()}]
    if history:
        for h in history[-8:]:
            role = "user" if h.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": h.get("content", "")[:2000]})
    messages.append({"role": "user", "content": user_msg[:2000]})
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.4,
    }).encode()
    req = _ureq.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with _ureq.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"]
            return text, None
    except _uerr.HTTPError as e:
        return None, "DeepSeek HTTP " + str(e.code) + ": " + e.read().decode()[:200]
    except Exception as e:
        return None, "DeepSeek error: " + type(e).__name__ + ": " + str(e)[:200]


def _ui_assistant_page():
    return ASSISTANT_HTML


ASSISTANT_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cognitive Core - AI помощник</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:linear-gradient(135deg,#0a0a14 0%,#1a1a2e 100%);
       color:#e8e8f0;min-height:100vh;display:flex;flex-direction:column}
  header{padding:14px 18px;background:rgba(255,255,255,.04);
         backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
         border-bottom:1px solid rgba(255,255,255,.08);
         display:flex;justify-content:space-between;align-items:center}
  header h1{font-size:16px;font-weight:600}
  header a{color:#9af;text-decoration:none;font-size:13px}
  #chat{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
  .msg{max-width:78%;padding:10px 14px;border-radius:18px;line-height:1.5;
       font-size:14px;word-wrap:break-word;white-space:pre-wrap}
  .msg.user{align-self:flex-end;background:#4a7dff;color:#fff;
            border-bottom-right-radius:4px}
  .msg.bot{align-self:flex-start;background:rgba(255,255,255,.08);
           backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
           border-bottom-left-radius:4px;
           border:1px solid rgba(255,255,255,.06)}
  .msg.bot code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:4px;
                font-family:monospace;font-size:12px}
  .msg.bot pre{background:rgba(0,0,0,.3);padding:10px;border-radius:8px;
               overflow-x:auto;margin:6px 0;font-size:12px}
  .typing{align-self:flex-start;color:#777;font-style:italic;font-size:13px;padding:8px 14px}
  form{padding:12px 16px;background:rgba(255,255,255,.04);
       backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
       border-top:1px solid rgba(255,255,255,.08);
       display:flex;gap:8px}
  input{flex:1;padding:10px 14px;border-radius:20px;border:1px solid rgba(255,255,255,.1);
        background:rgba(255,255,255,.05);color:#e8e8f0;font-size:14px;outline:none}
  input:focus{border-color:#4a7dff}
  button{padding:10px 20px;border-radius:20px;border:0;background:#4a7dff;
         color:#fff;font-weight:600;cursor:pointer;font-size:14px}
  button:disabled{background:#444;cursor:not-allowed}
  .quick{display:flex;flex-wrap:wrap;gap:6px;padding:0 18px 4px}
  .quick button{font-size:12px;padding:6px 12px;background:rgba(255,255,255,.08);
                border:1px solid rgba(255,255,255,.1)}
</style></head>
<body>
<header>
  <h1>AI помощник Cognitive Core</h1>
  <a href="/ui">Rooms UI</a>
</header>
<div id="chat">
  <div class="msg bot">Привет! Я помогу разобраться с Cognitive Core. Спроси что угодно - про установку, архитектуру, или как использовать с Claude Code.</div>
</div>
<div class="quick">
  <button onclick="ask('Что такое Cognitive Core простыми словами?')">Что это?</button>
  <button onclick="ask('Как установить за 60 секунд?')">Установка</button>
  <button onclick="ask('Как подключить Claude Code?')">Claude Code</button>
  <button onclick="ask('Как создать комнату и пригласить агента?')">Создать комнату</button>
  <button onclick="ask('Какие у проекта есть тарифы?')">Цены</button>
</div>
<form id="f" onsubmit="return send(event)">
  <input id="i" autocomplete="off" placeholder="Напиши вопрос..." autofocus>
  <button type="submit">Send</button>
</form>
<script>
const chat = document.getElementById('chat');
const inp = document.getElementById('i');
const btn = document.querySelector('form button');
const history = [];

function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

function ask(q) { inp.value = q; send(new Event('submit')); }

async function send(e) {
  e.preventDefault();
  const q = inp.value.trim();
  if (!q) return false;
  inp.value = ''; btn.disabled = true;
  addMsg('user', q);
  history.push({role:'user', content:q});
  const t = document.createElement('div');
  t.className = 'typing'; t.textContent = 'thinking...';
  chat.appendChild(t); chat.scrollTop = chat.scrollHeight;
  try {
    const r = await fetch('/ui/assistant/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:q, history:history.slice(0,-1)})
    });
    const j = await r.json();
    t.remove();
    if (j.error) addMsg('bot', 'Error: ' + j.error);
    else { addMsg('bot', j.reply); history.push({role:'assistant',content:j.reply}); }
  } catch(err) {
    t.remove(); addMsg('bot', 'Network error: ' + err.message);
  }
  btn.disabled = false; inp.focus();
  return false;
}
</script>
</body></html>"""

'''

# ===== Find insertion points =====
class_marker = "class Handler(http.server.BaseHTTPRequestHandler):"
assert class_marker in src, "Handler class not found"
src = src.replace(class_marker, helpers + "\n\n" + class_marker, 1)

# ===== Insert assistant route in do_GET (after /ui/answer block) =====
get_marker = '''            if path == "/ui/answer":
                # Form action handler — simple POST replacement
                room_key = params.get("key", [""])[0]
                qid = params.get("qid", [""])[0]
                agent = params.get("agent", [""])[0]
                self._send_html(200, _ui_answer_page(room_key, agent, qid))
                return'''

new_get = get_marker + '''

            if path == "/ui/assistant" or path == "/ui/assistant/":
                self._send_html(200, _ui_assistant_page())
                return'''

assert get_marker in src, "do_GET /ui/answer marker not found"
src = src.replace(get_marker, new_get, 1)

# ===== Insert /ui/assistant/chat route at start of do_POST routing =====
# Find: def do_POST(self):  ... then find first "if path ==" inside that method
post_def_idx = src.find("    def do_POST(self):")
assert post_def_idx > 0, "do_POST not found"

# Find first "if path ==" after do_POST def
search_from = post_def_idx
first_if_idx = src.find("if path ==", search_from)
assert first_if_idx > 0

new_post = '''if path == "/ui/assistant/chat":
                try:
                    body = json.loads(self._read_body() or "{}")
                    msg = body.get("message", "")[:2000]
                    history = body.get("history", [])
                    if not msg:
                        self._send(400, {"error": "missing message"})
                        return
                    text, err = _call_deepseek_chat(msg, history)
                    if err:
                        self._send(200, {"error": err})
                    else:
                        self._send(200, {"reply": text})
                except Exception as e:
                    self._send(500, {"error": type(e).__name__ + ": " + str(e)})
                return
            '''

src = src[:first_if_idx] + new_post + src[first_if_idx:]

open(p, "w").write(src)
print("OK patched -", len(open(p).read()), "bytes")
