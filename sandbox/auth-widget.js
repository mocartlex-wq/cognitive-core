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
    // Предпочитаем .top-status (стандартный контейнер top-bar)
    return document.querySelector('.top-status') ||
           document.querySelector('.top-nav') ||
           document.querySelector('.top-bar');
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
    const adminLabel = user.is_admin
      ? '<span class="cc-auth-admin-chip">Админ</span>'
      : '';
    wrap.innerHTML = `
      <button class="cc-auth-btn" type="button" aria-haspopup="true" aria-expanded="false">
        <span class="cc-auth-dot"></span>
        <span class="cc-auth-email">${escapeHtml(email)}</span>
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

  async function init() {
    const container = findContainer();
    if (!container) return;
    injectStyles();

    let status = null;
    try {
      // /auth/status — всегда 200 (с {authenticated: bool}), не 401.
      // Это чтобы browser console не загорался красным у незалогиненных юзеров.
      const r = await fetch('/auth/status', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (r.ok) {
        status = await r.json();
      }
    } catch (e) {
      // network failure — silent
      return;
    }

    if (status && status.authenticated && status.email) {
      renderLoggedIn(container, status);
      injectProfileNavLink();
    } else {
      renderLoggedOut(container);
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
