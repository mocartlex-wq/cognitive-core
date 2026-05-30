/* Cognitive Core — auth widget для top-bar
 *
 * Подключение: <script src="/static/auth-widget.js?v=20260520" defer></script>
 *
 * Поведение:
 *   1. На DOMContentLoaded дёргает GET /user/me (с cookie сессии)
 *   2. Если 200 OK — вставляет в .top-status кнопку «email · профиль»
 *      которая ведёт на /ui/profile + меню с logout
 *   3. Если 401 — вставляет кнопку «Войти» которая ведёт на /ui/login
 *   4. Если сеть упала — не показывает ничего (silent fail)
 *
 * Стилизация — встроенный <style>, чтобы не зависеть от глобальных CSS-файлов
 * (работает и без shared.css/glass.css если их нет).
 */
(function() {
  'use strict';

  // Не инициализируем дважды
  if (window.__ccAuthWidgetLoaded) return;
  window.__ccAuthWidgetLoaded = true;

  function injectStyles() {
    if (document.getElementById('cc-auth-widget-styles')) return;
    const css = `
      .cc-auth {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        position: relative;
      }
      .cc-auth-btn {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        padding: 6px 13px;
        height: 32px;
        background: rgba(88, 166, 255, 0.12);
        color: #58a6ff;
        border: 1px solid rgba(88, 166, 255, 0.3);
        border-radius: 8px;
        font-size: 13px;
        font-weight: 600;
        text-decoration: none;
        cursor: pointer;
        font-family: inherit;
        transition: background 0.15s, border-color 0.15s;
        white-space: nowrap;
      }
      .cc-auth-btn:hover {
        background: rgba(88, 166, 255, 0.2);
        border-color: rgba(88, 166, 255, 0.5);
      }
      .cc-auth-btn .cc-auth-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #3fb950;
        box-shadow: 0 0 5px rgba(63, 185, 80, 0.7);
      }
      .cc-auth-btn .cc-auth-email {
        max-width: 200px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .cc-auth-menu {
        position: absolute;
        top: calc(100% + 6px);
        right: 0;
        min-width: 200px;
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        box-shadow: 0 12px 32px rgba(0,0,0,0.6);
        padding: 6px;
        display: none;
        z-index: 1000;
      }
      :root[data-theme="light"] .cc-auth-menu {
        background: #fff;
        border-color: rgba(0,0,0,0.1);
        box-shadow: 0 12px 32px rgba(0,0,0,0.12);
      }
      .cc-auth-menu.open { display: block; }
      .cc-auth-menu a, .cc-auth-menu button {
        display: block;
        width: 100%;
        padding: 8px 12px;
        background: transparent;
        border: 0;
        color: inherit;
        text-align: left;
        font-size: 13.5px;
        font-family: inherit;
        text-decoration: none;
        border-radius: 6px;
        cursor: pointer;
      }
      .cc-auth-menu a:hover, .cc-auth-menu button:hover {
        background: rgba(88, 166, 255, 0.1);
      }
      .cc-auth-menu .cc-auth-divider {
        height: 1px;
        background: #30363d;
        margin: 4px 0;
      }
      :root[data-theme="light"] .cc-auth-menu .cc-auth-divider {
        background: rgba(0,0,0,0.08);
      }
      .cc-auth-menu .cc-auth-email-header {
        padding: 8px 12px 6px;
        font-size: 11.5px;
        color: rgba(255,255,255,0.55);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-weight: 600;
      }
      :root[data-theme="light"] .cc-auth-menu .cc-auth-email-header {
        color: rgba(0,0,0,0.5);
      }
      .cc-auth-admin-chip {
        display: inline-block;
        padding: 1px 6px;
        margin-left: 4px;
        background: rgba(255, 140, 66, 0.18);
        color: #ff8c42;
        border-radius: 999px;
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.3px;
      }
    `;
    const style = document.createElement('style');
    style.id = 'cc-auth-widget-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function findContainer() {
    // Предпочитаем .top-status (стандартный контейнер top-bar).
    const status = document.querySelector('.top-status');
    if (status) return status;
    // Если у страницы нет .top-status (напр. pricing) — НЕ суём аватар в .top-nav:
    // .top-nav абсолютно центрирован, лишний ребёнок сдвигает меню («прыжок» при
    // переходах). Вместо этого создаём .top-status внутри .top-bar.
    const bar = document.querySelector('.top-bar');
    if (bar) {
      const ts = document.createElement('div');
      ts.className = 'top-status';
      bar.appendChild(ts);
      return ts;
    }
    return document.querySelector('.top-nav') || document.body;
  }

  function renderLoggedOut(container) {
    const wrap = document.createElement('div');
    wrap.className = 'cc-auth';
    // Cache-bust query param: гарантирует свежий запрос даже если в
    // браузерном кеше залежался старый 404-ответ от устаревшего routing.
    const loginUrl = '/ui/login?_=' + Date.now();
    wrap.innerHTML = `<a class="cc-auth-btn" href="${loginUrl}">Войти</a>`;
    container.appendChild(wrap);
  }

  function renderLoggedIn(container, user) {
    const wrap = document.createElement('div');
    wrap.className = 'cc-auth';
    const email = user.email || '?';
    const displayName = user.display_name || email;
    const adminLabel = user.is_admin
      ? '<span class="cc-auth-admin-chip">Админ</span>'
      : '';
    // Avatar: initials + HSL цвет от user_id (синхронно с profile.html)
    function initials(src) {
      if (!src) return '?';
      const words = String(src).trim().split(/[\s_\-.]+/).filter(Boolean);
      if (words.length >= 2) return words.slice(0, 3).map(w => w[0].toUpperCase()).join('');
      const base = String(src).split('@')[0];
      return base.slice(0, 2).toUpperCase();
    }
    function colorFromId(id) {
      let h = 0;
      for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
      return `hsl(${h}, 60%, 55%)`;
    }
    const ini = initials(displayName);
    const bg = colorFromId(user.user_id || email);
    // Avatar circle вместо email-текста в top-bar (Owner-decision: «убери почту поставь аватар»)
    wrap.innerHTML = `
      <button class="cc-auth-btn" type="button" aria-haspopup="true" aria-expanded="false" title="${escapeHtml(displayName)}" style="padding:3px;background:transparent;border:0">
        <span class="cc-auth-dot" style="position:absolute;width:9px;height:9px;border:2px solid #0d1117;left:-2px;top:-2px"></span>
        <span class="cc-auth-avatar" style="width:32px;height:32px;border-radius:50%;background:${bg};display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:${ini.length > 2 ? 11 : 13}px;letter-spacing:0.3px;text-shadow:0 1px 2px rgba(0,0,0,0.3);position:relative">${escapeHtml(ini)}</span>
      </button>
      <div class="cc-auth-menu" role="menu">
        <div class="cc-auth-email-header">${escapeHtml(email)} ${adminLabel}</div>
        <div class="cc-auth-divider"></div>
        <a href="/ui/profile" role="menuitem">Профиль</a>
        <a href="/ui" role="menuitem">Мои комнаты</a>
        <div class="cc-auth-divider"></div>
        <button type="button" data-action="logout" role="menuitem">Выйти</button>
      </div>
    `;
    container.appendChild(wrap);

    const btn = wrap.querySelector('.cc-auth-btn');
    const menu = wrap.querySelector('.cc-auth-menu');
    const logoutBtn = wrap.querySelector('[data-action="logout"]');

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = menu.classList.contains('open');
      menu.classList.toggle('open', !isOpen);
      btn.setAttribute('aria-expanded', String(!isOpen));
    });

    // Закрываем меню по клику снаружи
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) {
        menu.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
      }
    });

    logoutBtn.addEventListener('click', async () => {
      try {
        await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
      } catch (e) {}
      // Сбрасываем кеш — иначе при возврате будет мигать старым email
      try { localStorage.removeItem(CACHE_KEY); } catch (e) {}
      location.href = '/ui/login';
    });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function injectProfileNavLink() {
    // Добавляем пункт «Профиль» в .top-nav если он там ещё не появлялся.
    // Это унифицирует навигацию: на любой странице залогиненный пользователь
    // видит ссылку на профиль в основном меню (а не только в dropdown'е).
    const nav = document.querySelector('.top-nav');
    if (!nav) return;
    if (nav.querySelector('a[href="/ui/profile"]')) {
      // Уже есть — пометим как active если мы сейчас на профиле
      if (location.pathname === '/ui/profile') {
        nav.querySelector('a[href="/ui/profile"]').classList.add('active');
      }
      return;
    }
    const a = document.createElement('a');
    a.href = '/ui/profile';
    a.textContent = 'Профиль';
    a.setAttribute('data-cc-injected', '1');
    if (location.pathname === '/ui/profile') a.classList.add('active');
    // Вставляем ПЕРЕД последним пунктом (обычно API) — единообразно с profile.html
    // где Профиль идёт предпоследним: «Главная · Комнаты · Профиль · API».
    const links = nav.querySelectorAll('a');
    const last = links[links.length - 1];
    if (last) {
      nav.insertBefore(a, last);
    } else {
      nav.appendChild(a);
    }
  }

  // Кеш статуса для мгновенного рендера при переходах между страницами.
  // Без него — на каждой новой странице top-status пустой пока fetch не вернётся
  // (50-200мс), и виджет «моргает». С кешем — мгновенный рендер из последнего
  // известного состояния, затем тихий update если что-то изменилось.
  const CACHE_KEY = 'cc_auth_status_cache_v1';

  function renderStatus(container, status) {
    // Если есть pre-render skeleton от head-bootstrap.js с подходящим состоянием —
    // не пересоздаём резким remove+create, просто заменяем skeleton в место полным
    // виджетом. Это устраняет визуальный скачок «skeleton → полный widget».
    //
    // Skeleton может быть двух видов:
    //   logged-in: <button> с .cc-auth-email содержащим email
    //   logged-out: <a> «Войти»
    // Match: skeleton-state совпадает с фактическим status.
    const pre = container.querySelector('.cc-auth[data-pre="1"]');
    let preMatches = false;
    if (pre && status) {
      // Новый skeleton использует .cc-auth-avatar (initials) вместо .cc-auth-email.
      // Logged-in: оба skeleton + status authenticated → match по user_id (если есть)
      // Logged-out: оба без avatar/email span → match.
      const preAvatar = pre.querySelector('.cc-auth-avatar');
      const preEmail = pre.querySelector('.cc-auth-email');  // legacy compat
      if (status.authenticated && status.email && (preAvatar || preEmail)) {
        preMatches = true;  // upgrade in-place — skeleton corrupt OR right, всё равно replace
      } else if (!status.authenticated && !preAvatar && !preEmail) {
        preMatches = true;  // оба logged-out
      }
    }

    if (preMatches) {
      // Upgrade in-place: убираем skeleton-метку и рендерим полноценный widget
      pre.remove();
    } else {
      // Полная пере-отрисовка (старая логика)
      container.querySelectorAll('.cc-auth').forEach(n => n.remove());
    }
    const oldProf = document.querySelector('a[data-cc-injected="1"]');
    if (oldProf) oldProf.remove();

    if (status && status.authenticated && status.email) {
      renderLoggedIn(container, status);
      injectProfileNavLink();
      // Retry на случай если nav был перерисован view-transition'ом
      // или DOM-сменой через bfcache restore — три попытки за 800мс.
      setTimeout(injectProfileNavLink, 80);
      setTimeout(injectProfileNavLink, 400);
      setTimeout(injectProfileNavLink, 800);
    } else {
      renderLoggedOut(container);
    }
  }

  function statusFingerprint(s) {
    if (!s) return 'null';
    if (!s.authenticated) return 'out';
    return 'in:' + (s.email || '') + ':' + (s.is_admin ? 'A' : 'U');
  }

  async function init() {
    const container = findContainer();
    if (!container) return;
    injectStyles();

    // Phase 1: мгновенный рендер из кеша (если есть).
    // Это устраняет «дырку» в top-status на 50-200мс при каждом переходе.
    let cached = null;
    try {
      cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
      if (cached) renderStatus(container, cached);
    } catch (e) {}

    // Phase 2: реальный запрос для проверки актуальности статуса.
    let status = null;
    try {
      const r = await fetch('/auth/status', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (r.ok) status = await r.json();
    } catch (e) {
      // Network failure — оставляем кешированный рендер, не перерисовываем.
      return;
    }
    if (!status) return;

    // Сохраняем актуальный статус в кеш для следующих переходов
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(status)); } catch (e) {}

    // Перерисовываем только если статус действительно изменился
    // (например logout в другой вкладке или session expired)
    if (statusFingerprint(cached) !== statusFingerprint(status)) {
      renderStatus(container, status);
    } else if (!cached) {
      // Первый визит — кеша не было, рисуем сейчас
      renderStatus(container, status);
    }
  }

  // BFCache (back/forward) handling: когда юзер возвращается на страницу
  // через назад/вперёд, браузер восстанавливает её из памяти БЕЗ повторного
  // запуска JS. Виджет уже был отрендерён — но статус мог измениться (logout
  // в другой вкладке). Перепроверяем при pageshow.persisted=true.
  window.addEventListener('pageshow', function(e) {
    if (e.persisted) {
      // Удаляем старый виджет (если был) и пересоздаём заново
      const old = document.querySelectorAll('.cc-auth');
      old.forEach(n => n.remove());
      init();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
