import test from 'node:test';
import assert from 'node:assert/strict';
import { createClaims } from '../lib/claims.mjs';
import { toNotification } from '../lib/poller.mjs';

const msg = { id: 'm1', text: '@Память проверь', from_agent: 'owner' };
const response = (status, data = {}) => ({ status, ok: status >= 200 && status < 300,
  json: async () => data });
function setup(fetchImpl, apiKey = 'fake-key') {
  const warnings = [];
  const claims = createClaims({ api: 'https://fake', self: 'dsdsd', apiKey, fetchImpl,
    warn: (s) => warnings.push(s) });
  return { claims, warnings };
}
const options = { api: 'https://fake', roomId: 'r1', roomKey: 'fake-room-key',
  fromAgent: 'dsdsd', text: 'готово' };

test('200: exact lock contract, string metadata, stable session and separate startups', async () => {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, ...opts, body: JSON.parse(opts.body) });
    return response(200, { scope: 'owner' });
  };
  const { claims } = setup(fetchImpl);
  const n = await claims.notification(msg, 'r1');
  assert.equal(n.meta.claimed, 'true');
  assert.equal(n.meta.claim_status, 'claimed');
  assert.equal(n.meta.claim_scope, 'owner');
  for (const v of Object.values(n.meta)) assert.equal(typeof v, 'string');
  assert.equal(calls[0].url, 'https://fake/coord/lock');
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].headers['X-API-Key'], 'fake-key');
  assert.equal(calls[0].headers['X-Room-Key'], undefined);
  assert.equal(calls[0].redirect, 'error');
  assert.ok(calls[0].signal instanceof AbortSignal);
  const holder = calls[0].body.holder;
  assert.match(holder, /^live:dsdsd:[a-f0-9-]{36}$/);
  assert.deepEqual(calls[0].body, { resource: 'reply:r1:m1', holder,
    ttl_seconds: 180, note: 'Ответ живой сессии Claude через cc-channel' });
  await claims.notification({ ...msg, text: '@dsdsd привет' }, 'r1');
  assert.equal(calls[1].body.holder, holder);
  await setup(fetchImpl).claims.notification(msg, 'r1');
  assert.notEqual(calls[2].body.holder, holder);
});

test('409: event still delivered, holder from detail.held_by', async () => {
  const { claims } = setup(async () => response(409, { detail: { held_by: 'fallback:other' } }));
  const n = await claims.notification(msg, 'r1');
  assert.equal(n.content, toNotification(msg, 'r1').content);
  assert.equal(n.meta.claimed, 'false');
  assert.equal(n.meta.claim_status, 'conflict');
  assert.equal(n.meta.held_by, 'fallback:other');
});

for (const [name, fetchImpl] of [
  ['network', async () => { throw new Error('offline'); }],
  ['HTTP 503', async () => response(503)],
  ['invalid JSON', async () => ({ status: 200, json: async () => { throw new Error('bad JSON'); } })],
]) test(`${name}: delivery preserved, claim explicitly unproven`, async () => {
  const n = await setup(fetchImpl).claims.notification(msg, 'r1');
  assert.equal(n.meta.claimed, 'false');
  assert.equal(n.meta.claim_status, 'unknown');
  assert.equal(n.meta.msg_id, 'm1');
  assert.equal(n.meta.held_by, undefined);
});

test('missing key: old metadata, no coord calls, exactly one startup warning', async () => {
  let count = 0;
  const { claims, warnings } = setup(async () => { count++; return response(200); }, '');
  assert.deepEqual(await claims.notification(msg, 'r1'), toNotification(msg, 'r1'));
  await claims.notification(msg, 'r1');
  assert.equal(count, 0);
  assert.equal((await claims.reply(options, 'm1')).receipt, 'skipped');
  assert.equal(count, 1); // room post only
  assert.equal(warnings.length, 1);
});

test('routing: unaddressed and self are claimed, other addressee is untouched', async () => {
  let count = 0;
  const { claims } = setup(async () => { count++; return response(200); });
  for (const text of ['без адресата', '@DSDSD!', '@Память, привет']) {
    assert.equal((await claims.notification({ ...msg, text }, 'r1')).meta.claimed, 'true');
  }
  for (const text of ['@Codex проверь', '@dsdsd-other проверь']) {
    const other = { ...msg, text };
    assert.deepEqual(await claims.notification(other, 'r1'), toNotification(other, 'r1'));
  }
  assert.equal(count, 3);
});

test('receipt success: only after post, original message ID and exact API fields', async () => {
  const calls = [];
  const { claims } = setup(async (url, opts) => {
    calls.push({ url, ...opts, body: JSON.parse(opts.body) });
    return response(200, { id: 'reply-id' });
  });
  const result = await claims.reply(options, 'original-id');
  assert.equal(result.receipt, 'sent');
  assert.deepEqual(calls.map((c) => c.url), ['https://fake/rooms/r1/post', 'https://fake/coord/receipt']);
  assert.deepEqual(calls[1].body, { message_id: 'original-id', actor: calls[1].body.actor,
    status: 'done', detail: 'Ответ отправлен в комнату r1' });
  assert.match(calls[1].body.actor, /^live:dsdsd:/);
  assert.equal(calls[1].headers['X-API-Key'], 'fake-key');
  assert.equal(calls[0].headers['X-API-Key'], undefined);
});

for (const network of [false, true]) test(`receipt failure (${network ? 'network' : 'HTTP'}): reply remains successful, no retry`, async () => {
  let count = 0;
  const { claims, warnings } = setup(async () => {
    count++;
    if (count === 1) return response(200, { ok: true });
    if (network) throw new Error('offline');
    return response(422);
  });
  const result = await claims.reply(options, 'm1');
  assert.equal(result.receipt, 'failed');
  assert.deepEqual(result.result, { ok: true });
  assert.equal(count, 2);
  assert.equal(warnings.length, 1);
});

test('failed room post never sends receipt', async () => {
  let count = 0;
  const { claims } = setup(async () => { count++; return response(500); });
  await assert.rejects(claims.reply(options, 'm1'), /HTTP 500/);
  assert.equal(count, 1);
});

test('missing original ID skips receipt even with key', async () => {
  let count = 0;
  const { claims } = setup(async () => { count++; return response(200); });
  assert.equal((await claims.reply(options)).receipt, 'skipped');
  assert.equal(count, 1);
});

test('isolated scope is visible, not advertised as cross-agent protection', async () => {
  const n = await setup(async () => response(200, { scope: 'agent' })).claims.notification(msg, 'r1');
  assert.equal(n.meta.claim_scope, 'agent');
});
