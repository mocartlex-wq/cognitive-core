// Чистая логика канала Cognitive Core: разбор конфигурации, long-poll комнат,
// отсев собственных сообщений и сборка уведомления. Без зависимостей и без
// MCP SDK — чтобы это можно было прогнать обычным node в тестах с фейковым fetch.

/** "id:key,id2:key2" → [{roomId, roomKey}]. Пустые и кривые записи отбрасываются. */
export function parseRooms(spec) {
  return String(spec || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((pair) => {
      const i = pair.indexOf(':');
      if (i <= 0) return null;
      const roomId = pair.slice(0, i).trim();
      const roomKey = pair.slice(i + 1).trim();
      return roomId && roomKey ? { roomId, roomKey } : null;
    })
    .filter(Boolean);
}

/** Один цикл ожидания. Возвращает массив сообщений (может быть пустым по таймауту). */
export async function waitOnce(fetchImpl, { api, roomId, roomKey, sinceSeconds = 5, timeout = 25 }) {
  const url = `${api}/rooms/${encodeURIComponent(roomId)}/wait`
    + `?since_seconds=${sinceSeconds}&timeout=${timeout}`;
  const res = await fetchImpl(url, { headers: { 'X-Room-Key': roomKey } });
  if (!res.ok) throw new Error(`wait ${roomId} → HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data && data.messages) ? data.messages : [];
}

/**
 * Что достойно уведомления: не наши собственные реплики и не то, что уже видели.
 * seen — Set идентификаторов, мутируется; ограничен, чтобы не течь в долгой сессии.
 */
export function selectNew(messages, { selfAgentId, seen, maxSeen = 500 }) {
  const out = [];
  for (const m of messages || []) {
    if (!m || !m.id) continue;
    if (seen.has(m.id)) continue;
    seen.add(m.id);
    if (selfAgentId && m.from_agent === selfAgentId) continue; // своё эхо
    if (!String(m.text || '').trim()) continue;
    out.push(m);
  }
  if (seen.size > maxSeen) {
    const drop = seen.size - maxSeen;
    let n = 0;
    for (const id of seen) { if (n++ >= drop) break; seen.delete(id); }
  }
  return out;
}

/** Сообщение комнаты → params для notifications/claude/channel. meta — только строки. */
export function toNotification(msg, roomId, roomName) {
  const sender = msg.display_name || msg.from_agent || 'unknown';
  const where = roomName ? `${roomName} (${roomId})` : roomId;
  return {
    content: `Сообщение из комнаты Cognitive Core ${where}\nОт: ${sender}\n\n${msg.text}`,
    meta: {
      room_id: String(roomId),
      sender: String(sender),
      msg_id: String(msg.id),
    },
  };
}

/** Ответ в комнату. Тело ровно то, что ждёт rooms-сервис: {from_agent, text}. */
export async function postReply(fetchImpl, { api, roomId, roomKey, fromAgent, text }) {
  if (!String(text || '').trim()) throw new Error('text обязателен и непустой');
  const res = await fetchImpl(`${api}/rooms/${encodeURIComponent(roomId)}/post`, {
    method: 'POST',
    headers: { 'X-Room-Key': roomKey, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from_agent: fromAgent, text }),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j && j.error) detail += ` — ${j.error}`; } catch { /* пусто */ }
    throw new Error(`post ${roomId} → ${detail}`);
  }
  return await res.json().catch(() => ({ ok: true }));
}
