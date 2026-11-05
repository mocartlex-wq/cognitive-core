import { randomUUID } from 'node:crypto';
import { toNotification, postReply } from './poller.mjs';

/** One holder per running channel, never shared with the fallback bridge. */
export function createClaims({ api, self, apiKey, fetchImpl = fetch, warn = console.error }) {
  const holder = `live:${self}:${randomUUID()}`;
  if (!apiKey) warn('[cogcore] COGCORE_API_KEY отсутствует — заявки и квитанции отключены');
  const post = (path, body) => fetchImpl(`${api}${path}`, {
    method: 'POST',
    headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(5000),
    redirect: 'error',
  });

  return {
    async notification(msg, roomId) {
      const params = toNotification(msg, roomId);
      if (!apiKey) return params;
      const mentions = [...String(msg.text || '').matchAll(/@([\p{L}\p{N}_-]+)/gu)]
        .map((m) => m[1].toLowerCase());
      const aliases = new Set([self.toLowerCase()]);
      if (['dsdsd', 'память'].includes(self.toLowerCase())) {
        aliases.add('dsdsd'); aliases.add('память');
      }
      if (mentions.length && !mentions.some((m) => aliases.has(m))) return params;
      try {
        const res = await post('/coord/lock', {
          resource: `reply:${roomId}:${msg.id}`, holder, ttl_seconds: 180,
          note: 'Ответ живой сессии Claude через cc-channel',
        });
        if (res.status === 200) {
          const data = await res.json();
          Object.assign(params.meta, { claimed: 'true', claim_status: 'claimed',
            claim_scope: String(data.scope || 'unknown') });
        } else if (res.status === 409) {
          const data = await res.json();
          Object.assign(params.meta, { claimed: 'false', claim_status: 'conflict',
            held_by: String(data?.detail?.held_by ?? '') });
        } else {
          Object.assign(params.meta, { claimed: 'false', claim_status: 'unknown' });
        }
      } catch {
        Object.assign(params.meta, { claimed: 'false', claim_status: 'unknown' });
      }
      return params;
    },

    async reply(options, messageId) {
      const result = await postReply(fetchImpl, options);
      if (!apiKey || !messageId) return { result, receipt: 'skipped' };
      try {
        const res = await post('/coord/receipt', {
          message_id: messageId, actor: holder, status: 'done',
          detail: `Ответ отправлен в комнату ${options.roomId}`,
        });
        if (res.ok) return { result, receipt: 'sent' };
      } catch { /* Reply already succeeded: never retry it due to receipt failure. */ }
      warn('[cogcore] Ответ отправлен, но квитанция /coord/receipt не подтверждена');
      return { result, receipt: 'failed' };
    },
  };
}
