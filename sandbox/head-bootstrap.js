/* Cognitive Core — head bootstrap (загружается СИНХРОННО в <head>)
 *
 * Решает две проблемы которые случаются ДО загрузки error-reporter.js
 * и auth-widget.js (они оба defer и грузятся после HTML parsing):
 *
 *  1) AbortError красный в DevTools console.
 *     view-transition'ы при быстрых переходах генерируют PromiseRejection
 *     "Transition was skipped". Если listener unhandledrejection не успел
 *     зацепиться — браузер логирует в console красным. Поэтому ставим
 *     preventDefault для безобидных rejections СИНХРОННО в <head>.
 *
 *  2) Мерцание auth-widget при переходе между страницами.
 *     auth-widget.js рендерит email только после DOMContentLoaded, и хотя
 *     там есть localStorage cache, view-transition берёт snapshot ДО того
 *     как defer-скрипт успел отработать → пользователь видит пустую правую
 *     часть top-bar секунду. Здесь мы synchronously читаем cache и
 *     встраиваем skeleton-разметку в .top-status — auth-widget потом её
 *     заменит на полноценный виджет с dropdown без визуального скачка.
 *
 * Никаких внешних зависимостей. ~50 строк.
 */
(function() {
  'use strict';

  // ─── 1. Подавление безобидных promise rejection (AbortError и т.п.) ────
  // listener ОБЯЗАН быть синхронно зарегистрирован в <head>, иначе error
  // от первого же click-transition попадёт в DevTools console красным.
  addEventListener('unhandledrejection', function(e) {
    var r = e.reason || {};
    var msg = String(r.message || r || '');
    if (r.name === 'AbortError' ||
        /transition was skipped|signal is aborted|aborted by the user|resizeobserver loop/i.test(msg)) {
      e.preventDefault();
    }
  });

  // ─── 2. Pre-render auth-widget из localStorage cache ───────────────────
  // Cache-key совпадает с auth-widget.js (CACHE_KEY = 'cc_auth_status_cache_v1').
  // Поддерживаем три состояния:
  //   logged-in (cached.authenticated && email) → email-кнопка skeleton
  //   logged-out (cached.authenticated === false) → «Войти» кнопка skeleton
  //   unknown (нет cache, первый визит) → НЕ рендерим — auth-widget сам решит
  // Цель: на каждой странице (кроме самого первого визита) — top-status
  // моментально занят правильным виджетом, БЕЗ дырки на 50-200ms.
  var cached;
  try {
    cached = JSON.parse(localStorage.getItem('cc_auth_status_cache_v1') || 'null');
  } catch (_) { cached = null; }
  if (!cached) return;  // первый визит — пусть auth-widget сам отработает

  var isLoggedIn = cached.authenticated && cached.email;
  var displayName = isLoggedIn ? (cached.display_name || cached.email) : '';

  // Avatar helpers (sync с auth-widget.js + profile.html)
  function initials(src) {
    if (!src) return '?';
    var words = String(src).trim().split(/[\s_\-.]+/).filter(Boolean);
    if (words.length >= 2) return words.slice(0, 3).map(function(w){return w[0].toUpperCase();}).join('');
    var base = String(src).split('@')[0];
    return base.slice(0, 2).toUpperCase();
  }
  function colorFromId(id) {
    var h = 0;
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
    return 'hsl(' + h + ', 60%, 55%)';
  }

  function renderSkeleton() {
    var status = document.querySelector('.top-status');
    if (!status) return;
    if (status.querySelector('.cc-auth')) return;
    var html;
    if (isLoggedIn) {
      var ini = initials(displayName);
      var bg = colorFromId(cached.user_id || cached.email);
      var iniEsc = ini.replace(/[&<>"']/g, function(ch) {
        return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[ch];
      });
      var fontSize = ini.length > 2 ? 11 : 13;
      html =
        '<div class="cc-auth" data-pre="1">' +
          '<button class="cc-auth-btn" type="button" aria-haspopup="true" aria-expanded="false" style="padding:3px;background:transparent;border:0">' +
            '<span class="cc-auth-dot" style="position:absolute;width:9px;height:9px;border:2px solid #0d1117;left:-2px;top:-2px"></span>' +
            '<span class="cc-auth-avatar" style="width:32px;height:32px;border-radius:50%;background:' + bg + ';display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:' + fontSize + 'px;letter-spacing:0.3px;text-shadow:0 1px 2px rgba(0,0,0,0.3);position:relative">' + iniEsc + '</span>' +
          '</button>' +
        '</div>';
    } else {
      html =
        '<div class="cc-auth" data-pre="1">' +
          '<a class="cc-auth-btn" href="/ui/login">Войти</a>' +
        '</div>';
    }
    status.insertAdjacentHTML('beforeend', html);
    if (!document.getElementById('cc-auth-pre-styles')) {
      var st = document.createElement('style');
      st.id = 'cc-auth-pre-styles';
      st.textContent =
        '.cc-auth{display:inline-flex;align-items:center;gap:4px;position:relative}' +
        '.cc-auth-btn{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;height:32px;' +
        'background:rgba(88,166,255,0.12);color:#58a6ff;border:1px solid rgba(88,166,255,0.3);' +
        'border-radius:8px;font-size:13px;font-weight:600;font-family:inherit;cursor:pointer;white-space:nowrap}' +
        '.cc-auth-dot{width:6px;height:6px;border-radius:50%;background:#3fb950;' +
        'box-shadow:0 0 5px rgba(63,185,80,0.7)}' +
        '.cc-auth-admin-chip{display:inline-block;padding:1px 6px;margin-left:4px;' +
        'background:rgba(255,140,66,0.18);color:#ff8c42;border-radius:999px;' +
        'font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.3px}';
      document.head.appendChild(st);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderSkeleton);
  } else {
    renderSkeleton();
  }
})();
