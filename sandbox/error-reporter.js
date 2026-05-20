/* Cognitive Core — Frontend error reporter
 *
 * Подключение: <script src="/static/error-reporter.js?v=20260520" defer></script>
 *
 * Ловит:
 *   • window.onerror — синхронные JS-ошибки (TypeError, ReferenceError, и т.д.)
 *   • window.onunhandledrejection — необработанные Promise rejection
 *   • fetch() с 5xx статусом — серверные ошибки на которые UI не среагировал
 *   • console.error — явные warnings
 *
 * Отправляет на POST /api/errors с throttling 1 событие/сек на клиенте +
 * deduplication (одинаковый message+url не шлётся повторно в течение минуты).
 *
 * Никаких PII не собирается. Содержимое страницы / DOM не отправляется.
 */
(function() {
  'use strict';

  if (window.__ccErrorReporterLoaded) return;
  window.__ccErrorReporterLoaded = true;

  const REPORTER_URL = '/api/errors';
  const MIN_INTERVAL_MS = 1000;     // 1 ошибка в секунду max
  const DEDUP_WINDOW_MS = 60 * 1000; // не дублировать одинаковые в течение минуты

  let lastSentAt = 0;
  const recentHashes = new Map(); // hash → timestamp

  function hashKey(payload) {
    return (payload.message || '').slice(0, 100) + '|' +
           (payload.source || '').slice(0, 80) + '|' +
           (payload.line || 0);
  }

  function send(payload) {
    const now = Date.now();
    if (now - lastSentAt < MIN_INTERVAL_MS) return;

    const key = hashKey(payload);
    const lastDup = recentHashes.get(key);
    if (lastDup && (now - lastDup) < DEDUP_WINDOW_MS) return;
    recentHashes.set(key, now);

    // Cleanup старых записей
    if (recentHashes.size > 100) {
      const cutoff = now - DEDUP_WINDOW_MS;
      for (const [k, ts] of recentHashes) {
        if (ts < cutoff) recentHashes.delete(k);
      }
    }

    lastSentAt = now;

    payload.url = String(location.href || '').slice(0, 500);
    payload.user_agent = String(navigator.userAgent || '').slice(0, 400);
    payload.referrer = String(document.referrer || '').slice(0, 500);
    payload.viewport_w = window.innerWidth;
    payload.viewport_h = window.innerHeight;
    payload.client_ts = now;

    // sendBeacon если доступен (надёжнее — отправит даже при unload)
    try {
      if (navigator.sendBeacon) {
        const blob = new Blob([JSON.stringify(payload)],
                              { type: 'application/json' });
        const ok = navigator.sendBeacon(REPORTER_URL, blob);
        if (ok) return;
      }
    } catch (e) {}

    // fallback на fetch
    try {
      fetch(REPORTER_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'same-origin',
        keepalive: true,
      }).catch(() => {});  // silent — мы не должны падать при отправке ошибки
    } catch (e) {}
  }

  // ─── 1. Глобальные JS ошибки ───────────────────────────────────────────
  window.addEventListener('error', function(e) {
    // Ошибки загрузки ресурсов (img, script, link) — events с target ≠ window
    if (e.target && e.target !== window && (e.target.src || e.target.href)) {
      const url = e.target.src || e.target.href;
      // Игнорируем 404 на favicon — не интересно (он же на бэке решён)
      if (/favicon/i.test(url)) return;
      send({
        message: 'Resource load failed: ' + (e.target.tagName || '?'),
        source: String(url).slice(0, 300),
        error_kind: 'resource',
      });
      return;
    }
    send({
      message: String(e.message || e.error || 'unknown').slice(0, 1000),
      stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 2000) : null,
      source: String(e.filename || '').slice(0, 300),
      line: e.lineno || 0,
      col: e.colno || 0,
      error_kind: 'js',
    });
  }, true);

  // ─── 2. Необработанные Promise rejection ───────────────────────────────
  window.addEventListener('unhandledrejection', function(e) {
    let msg = 'unhandled promise rejection';
    let stack = null;
    if (e.reason) {
      if (typeof e.reason === 'string') msg = e.reason;
      else if (e.reason.message) {
        msg = e.reason.message;
        stack = e.reason.stack || null;
      } else {
        try { msg = JSON.stringify(e.reason); } catch (_) { msg = String(e.reason); }
      }
    }
    send({
      message: String(msg).slice(0, 1000),
      stack: stack ? String(stack).slice(0, 2000) : null,
      error_kind: 'promise',
    });
  });

  // ─── 3. Fetch с 5xx статусом ───────────────────────────────────────────
  if (window.fetch) {
    const origFetch = window.fetch.bind(window);
    window.fetch = async function(input, init) {
      try {
        const response = await origFetch(input, init);
        // Только 5xx сообщаем (4xx — клиентские, может быть ожидаемо)
        if (response.status >= 500 && response.status < 600) {
          const url = typeof input === 'string' ? input :
                      (input && input.url) ? input.url : '?';
          // Не репортить ошибки самого репортера, чтобы не было loop
          if (String(url).indexOf(REPORTER_URL) === -1) {
            send({
              message: 'HTTP ' + response.status + ' ' + (response.statusText || ''),
              source: String(url).slice(0, 300),
              error_kind: 'fetch',
            });
          }
        }
        return response;
      } catch (e) {
        const url = typeof input === 'string' ? input :
                    (input && input.url) ? input.url : '?';
        if (String(url).indexOf(REPORTER_URL) === -1) {
          send({
            message: 'Fetch failed: ' + String(e.message || e).slice(0, 200),
            source: String(url).slice(0, 300),
            error_kind: 'fetch',
            stack: e.stack ? String(e.stack).slice(0, 2000) : null,
          });
        }
        throw e;
      }
    };
  }
})();
