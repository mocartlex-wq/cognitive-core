/* Service worker для /chat.
 *
 * Отдаётся с корня (`GET /sw.js` в app/main.py), а не из /static/: область
 * действия воркера не может быть шире каталога, из которого он отдан, и из
 * /static/ он не покрыл бы ни /chat, ни уведомления.
 *
 * Стратегия сознательно сетевая. Воркер живёт в браузере до явного удаления:
 * если он начнёт отдавать закэшированную оболочку раньше сети, сломанная
 * версия переживёт исправление на сервере, и починить её со стороны сервера
 * будет нечем. Поэтому кэш здесь — только запасной выход при отсутствии сети.
 */
const VERSION = 'cc-v1';
const SHELL = 'cc-shell-' + VERSION;
const OFFLINE_URL = '/static/offline.html';
const PRECACHE = [
  OFFLINE_URL,
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    // Отдельными запросами: один недоступный файл не должен ронять установку
    // целиком, иначе воркер не встанет вовсе и молча.
    await Promise.all(PRECACHE.map((u) => cache.add(u).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n !== SHELL).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

// Всё, что похоже на данные, воркер не трогает вообще: закэшированный ответ
// API — это показанная вчерашняя переписка без единого признака, что она
// вчерашняя.
function isData(url) {
  return /^\/(user|rooms|api|auth|operative|memory)\//.test(url.pathname);
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || isData(url)) return;

  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        return await fetch(req);
      } catch (err) {
        return (await caches.match(OFFLINE_URL)) ||
               new Response('Нет сети', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
      }
    })());
    return;
  }

  // Статика: сначала сеть, кэш обновляем на успехе. Оффлайн отдаём копию.
  e.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res && res.ok) {
        const cache = await caches.open(SHELL);
        cache.put(req, res.clone());
      }
      return res;
    } catch (err) {
      const hit = await caches.match(req);
      if (hit) return hit;
      throw err;
    }
  })());
});

// ─── Уведомления ────────────────────────────────────────────────────────────
// Обработчики стоят заранее и без подписки безвредны: подписка на push
// добавляется отдельно, а без неё событие push просто не приходит.
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  const title = d.title || 'Cognitive Core';
  e.waitUntil(self.registration.showNotification(title, {
    body: (d.body || '').slice(0, 200),
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.tag || 'cc-room',
    renotify: true,
    data: { url: d.url || '/chat' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/chat';
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      // Уже открытую вкладку поднимаем, а не плодим вторую.
      if (c.url.includes('/chat') && 'focus' in c) return c.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
