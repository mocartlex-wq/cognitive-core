/* Cognitive Core — плавающий ассистент (floating assistant widget).
 *
 * Небольшое окно поверх любой страницы сайта (как у Claude): кнопка-лаунчер
 * внизу справа открывает компактную панель с чатом помощников. Вкладка-полоса
 * сверху оставлена под будущие инструменты (расширяемость).
 *
 * Самодостаточный — без зависимостей. Общается с оркестратором на том же
 * origin: /orchestrator/session-login (SSO по cookie сайта), /orchestrator/ask,
 * /orchestrator/tasks/{id}. Токен кэшируется в localStorage('orch_token').
 *
 * Подключается авто-инъекцией в _html() (app/main.py) на страницах сайта.
 */
(function () {
  "use strict";
  if (window.__cogAsst) return;            // single instance
  window.__cogAsst = true;
  if (window.top !== window.self) return;  // not inside an iframe
  var path = location.pathname.replace(/\/+$/, "");
  // Не показываем на самой странице чата и на входе (там вход уже есть).
  if (path === "/ui/ask" || path.indexOf("/ui/login") === 0) return;

  var ORCH = location.origin.replace(/\/$/, "") + "/orchestrator";
  var token = localStorage.getItem("orch_token") || "";
  var authed = false, busy = false, opened = false, greeted = false;

  // Выбор помощника. «auto» — оркестратор (несколько ИИ, нужен вход).
  // Остальные — конкретные персоны через /ui/team/chat (синхронно, без входа).
  var PERSONAS = [
    { id: "auto",      label: "✨ Авто (несколько ИИ)" },
    { id: "developer", label: "💻 Разработчик" },
    { id: "designer",  label: "🎨 UX/UI дизайнер" },
    { id: "content",   label: "✍️ Контент-редактор" },
    { id: "security",  label: "🔐 Безопасник" },
    { id: "support",   label: "📖 Поддержка пользователей" },
  ];
  var persona = localStorage.getItem("cogasst_persona") || "auto";
  if (!PERSONAS.some(function (p) { return p.id === persona; })) persona = "auto";
  var convo = [];  // [{role:'user'|'assistant', content}]  — общий контекст диалога

  // ─── styles ──────────────────────────────────────────────────────────────
  var CSS =
  "#cogasst-fab{position:fixed;right:24px;bottom:24px;width:58px;height:58px;border:0;border-radius:50%;" +
    "background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;cursor:pointer;z-index:2147483000;" +
    "box-shadow:0 10px 28px rgba(99,102,241,.45);display:flex;align-items:center;justify-content:center;" +
    "transition:transform .15s ease,box-shadow .15s ease;padding:0;}" +
  "#cogasst-fab:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 14px 34px rgba(99,102,241,.55);}" +
  "#cogasst-fab svg{width:27px;height:27px;}" +
  "#cogasst-fab .cogasst-dot{position:absolute;top:12px;right:12px;width:9px;height:9px;border-radius:50%;" +
    "background:#34c759;box-shadow:0 0 0 3px rgba(52,199,89,.25);}" +
  "#cogasst-panel{position:fixed;right:24px;bottom:92px;width:384px;max-width:calc(100vw - 32px);" +
    "height:min(620px,78vh);z-index:2147483000;display:none;flex-direction:column;overflow:hidden;" +
    "border-radius:20px;border:1px solid var(--glass-border,rgba(255,255,255,.12));" +
    "background:var(--glass-base-bg,#11141c);color:var(--glass-text,#e9edf5);" +
    "box-shadow:0 24px 64px rgba(0,0,0,.5);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
    "opacity:0;transform:translateY(12px) scale(.98);transition:opacity .16s ease,transform .16s ease;}" +
  "#cogasst-panel.cogasst-on{display:flex;opacity:1;transform:none;}" +
  ".cogasst-head{display:flex;align-items:center;gap:10px;padding:13px 14px;border-bottom:1px solid var(--glass-border,rgba(255,255,255,.1));}" +
  ".cogasst-head .cogasst-ic{width:30px;height:30px;border-radius:9px;flex:0 0 auto;background:linear-gradient(135deg,#6366f1,#a855f7);" +
    "display:flex;align-items:center;justify-content:center;color:#fff;}" +
  ".cogasst-head .cogasst-ic svg{width:17px;height:17px;}" +
  ".cogasst-title{font-weight:700;font-size:15px;flex:1;line-height:1.15;}" +
  ".cogasst-title small{display:block;font-weight:500;font-size:11px;opacity:.6;}" +
  ".cogasst-x{background:transparent;border:0;color:inherit;opacity:.55;cursor:pointer;font-size:22px;line-height:1;padding:4px 6px;border-radius:8px;}" +
  ".cogasst-x:hover{opacity:1;background:rgba(255,255,255,.08);}" +
  ".cogasst-tabs{display:flex;gap:6px;padding:8px 12px 0;}" +
  ".cogasst-tab{font-size:12.5px;font-weight:600;padding:6px 12px;border-radius:9px 9px 0 0;border:0;cursor:pointer;" +
    "background:transparent;color:var(--glass-text,#e9edf5);opacity:.65;}" +
  ".cogasst-tab.cogasst-act{opacity:1;background:var(--glass-bg-light,rgba(255,255,255,.06));}" +
  ".cogasst-tab.cogasst-soon{opacity:.4;cursor:default;}" +
  ".cogasst-pbar{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--glass-border,rgba(255,255,255,.1));}" +
  ".cogasst-pbar label{font-size:11.5px;opacity:.6;flex:0 0 auto;}" +
  ".cogasst-pbar select{flex:1;font:inherit;font-size:13px;padding:7px 10px;border-radius:10px;cursor:pointer;" +
    // Solid colours (NOT translucent): the native option popup renders on the
    // browser's own background — translucent/inherited light text became
    // white-on-white and unreadable. Explicit dark bg + light text fixes it.
    "background:#1b1f2a;color:#e9edf5;" +
    "border:1px solid var(--glass-border,rgba(255,255,255,.14));-webkit-appearance:none;appearance:none;}" +
  ".cogasst-pbar option{background:#1b1f2a;color:#e9edf5;}" +
  ".cogasst-pbar select:focus{outline:none;border-color:#6366f1;}" +
  ".cogasst-body{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;}" +
  ".cogasst-msg{max-width:86%;padding:9px 12px;border-radius:13px;white-space:pre-wrap;word-wrap:break-word;}" +
  ".cogasst-msg.u{align-self:flex-end;background:#2f6fed;color:#fff;border-bottom-right-radius:4px;}" +
  ".cogasst-msg.a{align-self:flex-start;background:var(--glass-bg-light,rgba(255,255,255,.06));" +
    "border:1px solid var(--glass-border,rgba(255,255,255,.1));border-bottom-left-radius:4px;}" +
  ".cogasst-msg.sys{align-self:center;font-size:12.5px;opacity:.6;background:transparent;text-align:center;}" +
  ".cogasst-gate{align-self:stretch;text-align:center;padding:18px 10px;display:flex;flex-direction:column;gap:12px;align-items:center;}" +
  ".cogasst-gate a{background:#2f6fed;color:#fff;text-decoration:none;padding:11px 22px;border-radius:11px;font-weight:600;}" +
  ".cogasst-foot{border-top:1px solid var(--glass-border,rgba(255,255,255,.1));padding:10px 12px;display:flex;gap:8px;align-items:flex-end;}" +
  ".cogasst-foot textarea{flex:1;resize:none;max-height:120px;min-height:40px;border-radius:11px;padding:9px 11px;font:inherit;" +
    "background:var(--glass-bg-light,rgba(255,255,255,.06));color:inherit;" +
    "border:1px solid var(--glass-border,rgba(255,255,255,.14));}" +
  ".cogasst-foot textarea:focus{outline:none;border-color:#6366f1;}" +
  ".cogasst-send{flex:0 0 auto;width:40px;height:40px;border-radius:11px;border:0;cursor:pointer;color:#fff;font-size:18px;" +
    "background:linear-gradient(135deg,#6366f1,#a855f7);}" +
  ".cogasst-send:disabled{opacity:.5;cursor:not-allowed;}" +
  ".cogasst-spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.25);border-top-color:#a855f7;" +
    "border-radius:50%;animation:cogasstspin .8s linear infinite;vertical-align:middle;margin-right:6px;}" +
  "@keyframes cogasstspin{to{transform:rotate(360deg);}}" +
  "@media(max-width:480px){#cogasst-panel{right:8px;left:8px;width:auto;bottom:84px;height:74vh;}}";

  var st = document.createElement("style"); st.textContent = CSS; document.head.appendChild(st);

  var CHAT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';

  // ─── DOM ─────────────────────────────────────────────────────────────────
  var fab = document.createElement("button");
  fab.id = "cogasst-fab"; fab.title = "Помощники AI"; fab.setAttribute("aria-label", "Открыть помощников");
  fab.innerHTML = CHAT_SVG + '<span class="cogasst-dot"></span>';

  var panel = document.createElement("div");
  panel.id = "cogasst-panel";
  panel.innerHTML =
    '<div class="cogasst-head">' +
      '<div class="cogasst-ic">' + CHAT_SVG + "</div>" +
      '<div class="cogasst-title">Помощники AI<small>Cognitive Core</small></div>' +
      '<button class="cogasst-x" aria-label="Закрыть">&times;</button>' +
    "</div>" +
    '<div class="cogasst-tabs">' +
      '<button class="cogasst-tab cogasst-act" data-tab="chat">Чат</button>' +
      '<button class="cogasst-tab cogasst-soon" title="Скоро: новые инструменты">+ инструменты</button>' +
    "</div>" +
    '<div class="cogasst-pbar">' +
      '<label for="cogasst-persona">Помощник:</label>' +
      '<select id="cogasst-persona">' +
        PERSONAS.map(function (p) {
          return '<option value="' + p.id + '"' + (p.id === persona ? " selected" : "") + ">" + p.label + "</option>";
        }).join("") +
      "</select>" +
    "</div>" +
    '<div class="cogasst-body" id="cogasst-body"></div>' +
    '<div class="cogasst-foot">' +
      '<textarea id="cogasst-input" rows="1" placeholder="Спросите помощников…"></textarea>' +
      '<button class="cogasst-send" id="cogasst-send" aria-label="Отправить">&#8594;</button>' +
    "</div>";

  function mount() { document.body.appendChild(fab); document.body.appendChild(panel); wire(); }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);

  // ─── helpers ───────────────────────────────────────────────────────────────
  var bodyEl, inputEl, sendEl;
  function addMsg(kind, text) {
    var el = document.createElement("div");
    el.className = "cogasst-msg " + kind; el.textContent = text || "";
    bodyEl.appendChild(el); bodyEl.scrollTop = bodyEl.scrollHeight; return el;
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // Подсказка про вход — только для режима «Авто» (оркестратор). Не затирает
  // диалог: добавляется как сообщение, селектор персон остаётся рабочим.
  function gateHint() {
    var g = document.createElement("div"); g.className = "cogasst-gate";
    var p = document.createElement("div");
    p.textContent = "Режим «Авто» требует входа. Войдите аккаунтом сайта — или выберите конкретного помощника выше, он ответит без входа.";
    var a = document.createElement("a");
    a.href = "/ui/login?next=" + encodeURIComponent(location.pathname);
    a.textContent = "Войти через почту";
    g.appendChild(p); g.appendChild(a); bodyEl.appendChild(g);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  async function ensureAuth() {
    if (authed && token) return true;
    if (token) { authed = true; return true; }
    try {
      var r = await fetch(ORCH + "/session-login", { method: "POST", credentials: "same-origin" });
      if (r.ok) {
        var d = await r.json();
        token = d.token || ""; if (token) localStorage.setItem("orch_token", token);
        authed = !!token; return authed;
      }
    } catch (e) { /* offline / fall through */ }
    return false;
  }

  function greet() {
    if (greeted) return;
    greeted = true; bodyEl.innerHTML = "";
    addMsg("sys", "Здравствуйте! Выберите помощника выше или оставьте «Авто» — и задайте вопрос.");
  }

  async function openPanel() {
    opened = true; panel.classList.add("cogasst-on"); fab.style.display = "none";
    greet();
    // Тихо пробуем SSO для режима «Авто» — не блокируем, персоны работают и без входа.
    ensureAuth();
    setTimeout(function () { try { inputEl.focus(); } catch (e) {} }, 50);
  }
  function closePanel() { opened = false; panel.classList.remove("cogasst-on"); fab.style.display = "flex"; }

  function finish(ph, answer) {
    ph.innerHTML = ""; ph.textContent = answer;
    convo.push({ role: "assistant", content: answer });
    if (convo.length > 40) convo = convo.slice(-40);
    bodyEl.scrollTop = bodyEl.scrollHeight;
    busy = false; sendEl.disabled = false;
  }

  async function send() {
    var text = (inputEl.value || "").trim();
    if (!text || busy) return;
    greet();
    inputEl.value = ""; inputEl.style.height = "auto";
    addMsg("u", text);

    if (persona === "auto") {
      // Режим «Авто» → оркестратор (нужен вход).
      if (!(await ensureAuth())) { gateHint(); return; }
      busy = true; sendEl.disabled = true;
      convo.push({ role: "user", content: text });
      var ph = addMsg("a", ""); ph.innerHTML = '<span class="cogasst-spin"></span>Обрабатываю…';
      var taskId = "";
      try {
        var r = await fetch(ORCH + "/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-User-Token": token },
          body: JSON.stringify({ request: text }),
        });
        if (r.status === 401) { token = ""; authed = false; localStorage.removeItem("orch_token"); ph.remove(); busy = false; sendEl.disabled = false; gateHint(); return; }
        taskId = (await r.json()).task_id || "";
      } catch (e) {
        ph.textContent = "Не удалось отправить запрос. Проверьте интернет."; busy = false; sendEl.disabled = false; return;
      }
      if (!taskId) { ph.textContent = "Сервис временно недоступен. Попробуйте позже."; busy = false; sendEl.disabled = false; return; }
      for (var i = 0; i < 80; i++) {
        await sleep(2500);
        try {
          var r2 = await fetch(ORCH + "/tasks/" + taskId, { headers: { "X-User-Token": token } });
          if (!r2.ok) continue;
          var d = await r2.json();
          if (d.status === "completed" || d.status === "failed") { finish(ph, d.final_answer || "(пустой ответ)"); return; }
        } catch (e) { /* keep polling */ }
      }
      ph.textContent = "Ответ готовится дольше обычного. Загляните чуть позже."; busy = false; sendEl.disabled = false;
      return;
    }

    // Конкретная персона → /ui/team/chat (синхронно, без входа).
    busy = true; sendEl.disabled = true;
    var hist = convo.slice(-8);
    convo.push({ role: "user", content: text });
    var ph2 = addMsg("a", ""); ph2.innerHTML = '<span class="cogasst-spin"></span>Печатает…';
    try {
      var rp = await fetch("/ui/team/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona: persona, message: text, history: hist }),
      });
      var dp = await rp.json().catch(function () { return {}; });
      if (rp.ok && dp.reply) { finish(ph2, dp.reply); }
      else { ph2.textContent = dp.error || "Помощник сейчас недоступен. Попробуйте позже."; busy = false; sendEl.disabled = false; }
    } catch (e) {
      ph2.textContent = "Не удалось отправить запрос. Проверьте интернет."; busy = false; sendEl.disabled = false;
    }
  }

  function wire() {
    bodyEl = panel.querySelector("#cogasst-body");
    inputEl = panel.querySelector("#cogasst-input");
    sendEl = panel.querySelector("#cogasst-send");
    fab.addEventListener("click", openPanel);
    panel.querySelector(".cogasst-x").addEventListener("click", closePanel);
    sendEl.addEventListener("click", send);
    var sel = panel.querySelector("#cogasst-persona");
    if (sel) sel.addEventListener("change", function () {
      persona = this.value; localStorage.setItem("cogasst_persona", persona);
      var label = (PERSONAS.find(function (p) { return p.id === persona; }) || {}).label || persona;
      addMsg("sys", persona === "auto" ? "Режим «Авто»: вопрос уйдёт подходящему помощнику." : "Теперь отвечает: " + label);
    });
    inputEl.addEventListener("input", function () { this.style.height = "auto"; this.style.height = Math.min(this.scrollHeight, 120) + "px"; });
    inputEl.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && opened) closePanel(); });
  }
})();
