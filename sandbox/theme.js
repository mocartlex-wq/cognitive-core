/* Cognitive Core — theme switcher
   - Auto-detect через prefers-color-scheme
   - Manual toggle через localStorage
   - Кнопка .theme-toggle в шапке переключает на лету
*/
(function() {
  const STORAGE_KEY = 'cc_theme';
  const html = document.documentElement;

  function apply(theme) {
    if (theme === 'light' || theme === 'dark') {
      html.setAttribute('data-theme', theme);
    } else {
      html.removeAttribute('data-theme');
    }
  }

  // Initial: localStorage > system
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) apply(saved);

  // Public API
  window.toggleTheme = function() {
    const current = html.getAttribute('data-theme');
    let next;
    if (current === 'light') next = 'dark';
    else if (current === 'dark') next = 'light';
    else {
      // No data-theme → берём ОБРАТНОЕ системному
      const isLight = window.matchMedia('(prefers-color-scheme: light)').matches;
      next = isLight ? 'dark' : 'light';
    }
    apply(next);
    localStorage.setItem(STORAGE_KEY, next);
  };

  // Auto-bootstrap auth-widget и error-reporter (если их ещё нет на странице).
  // Это страховка: если HTML страницы — устаревший в браузерном кеше и не
  // содержит <script src="auth-widget.js">, theme.js сам её подгрузит.
  // Старые версии theme.js не имеют этого блока, но как только пользователь
  // получит свежий theme.js (через Ctrl+Shift+R), бутстрап начнёт работать
  // постоянно — на всех будущих страницах виджет появится сам.
  function ensureScript(src, marker) {
    if (window[marker]) return;
    if (document.querySelector('script[data-cc-auto="' + marker + '"]')) return;
    const s = document.createElement('script');
    s.src = src;
    s.defer = true;
    s.setAttribute('data-cc-auto', marker);
    document.head.appendChild(s);
  }

  // На каждой странице с .top-bar — нужен auth-widget. На каждой — error-reporter.
  document.addEventListener('DOMContentLoaded', () => {
    if (document.querySelector('.top-bar, .top-status')) {
      ensureScript('/static/auth-widget.js?v=20260520f', '__ccAuthWidgetLoaded');
    }
    ensureScript('/static/error-reporter.js?v=20260520b', '__ccErrorReporterLoaded');
  });

  // Inject neuron background и icon sprite
  document.addEventListener('DOMContentLoaded', () => {
    if (!document.querySelector('.neuron-bg')) {
      const wrap = document.createElement('div');
      wrap.className = 'neuron-bg';
      const obj = document.createElement('object');
      obj.type = 'image/svg+xml';
      obj.data = '/static/neurons.svg';
      obj.setAttribute('aria-hidden', 'true');
      wrap.appendChild(obj);
      document.body.insertBefore(wrap, document.body.firstChild);
    }
    // Inline icon sprite (для <use href="#name">)
    if (!document.getElementById('cc-icons-sprite')) {
      fetch('/static/icons.svg')
        .then(r => r.text())
        .then(svg => {
          const wrap = document.createElement('div');
          wrap.id = 'cc-icons-sprite';
          wrap.style.display = 'none';
          wrap.innerHTML = svg;
          document.body.appendChild(wrap);
        })
        .catch(() => {});
    }
  });
})();
