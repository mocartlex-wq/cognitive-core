// Смоук на чистом node с фейковым fetch — без сети и без MCP SDK.
// Запуск: node test/smoke.mjs
import assert from 'node:assert/strict';
import { parseRooms, waitOnce, selectNew, toNotification, postReply } from '../lib/poller.mjs';

let passed = 0;
const ok = (name) => { console.log(`  ok  ${name}`); passed++; };

// --- parseRooms -------------------------------------------------------------
assert.deepEqual(parseRooms('a:1,b:2'), [{ roomId: 'a', roomKey: '1' }, { roomId: 'b', roomKey: '2' }]);
assert.deepEqual(parseRooms(''), []);
assert.deepEqual(parseRooms(undefined), []);
assert.deepEqual(parseRooms('  a:1 , broken , :nokey , id: '), [{ roomId: 'a', roomKey: '1' }]);
// ключ с двоеточиями внутри не должен резаться
assert.deepEqual(parseRooms('r:rk_a:b:c'), [{ roomId: 'r', roomKey: 'rk_a:b:c' }]);
ok('parseRooms разбирает и отбрасывает мусор');

// --- waitOnce ---------------------------------------------------------------
{
  let seenUrl = null, seenHeaders = null;
  const fakeFetch = async (url, opts) => {
    seenUrl = url; seenHeaders = opts.headers;
    return { ok: true, json: async () => ({ messages: [{ id: 'm1', text: 'hi' }], timeout: false }) };
  };
  const msgs = await waitOnce(fakeFetch, { api: 'https://x', roomId: 'r1', roomKey: 'k1', sinceSeconds: 5, timeout: 25 });
  assert.equal(msgs.length, 1);
  assert.match(seenUrl, /^https:\/\/x\/rooms\/r1\/wait\?since_seconds=5&timeout=25$/);
  assert.equal(seenHeaders['X-Room-Key'], 'k1');
  ok('waitOnce строит URL и шлёт X-Room-Key');
}
{
  const fakeFetch = async () => ({ ok: false, status: 401, json: async () => ({}) });
  await assert.rejects(() => waitOnce(fakeFetch, { api: 'https://x', roomId: 'r1', roomKey: 'bad' }), /HTTP 401/);
  ok('waitOnce падает на не-2xx');
}
{
  const fakeFetch = async () => ({ ok: true, json: async () => ({ timeout: true }) });
  assert.deepEqual(await waitOnce(fakeFetch, { api: 'https://x', roomId: 'r', roomKey: 'k' }), []);
  ok('waitOnce переживает ответ по таймауту без messages');
}

// --- selectNew --------------------------------------------------------------
{
  const seen = new Set();
  const msgs = [
    { id: '1', from_agent: 'owner:me', text: 'привет' },
    { id: '2', from_agent: 'claude-code', text: 'моё эхо' },
    { id: '3', from_agent: 'owner:me', text: '   ' },
    { id: '1', from_agent: 'owner:me', text: 'дубль' },
  ];
  const got = selectNew(msgs, { selfAgentId: 'claude-code', seen });
  assert.deepEqual(got.map((m) => m.id), ['1']);
  assert.deepEqual(selectNew(msgs, { selfAgentId: 'claude-code', seen }).map((m) => m.id), []);
  ok('selectNew отсеивает своё эхо, пустые и повторы');
}
{
  const seen = new Set();
  const many = Array.from({ length: 60 }, (_, i) => ({ id: `x${i}`, from_agent: 'o', text: 't' }));
  selectNew(many, { selfAgentId: 'me', seen, maxSeen: 10 });
  assert.ok(seen.size <= 10, `seen должен быть ограничен, получили ${seen.size}`);
  ok('selectNew не даёт seen расти без предела');
}

// --- toNotification ---------------------------------------------------------
{
  const p = toNotification({ id: 'm9', from_agent: 'owner:me', display_name: 'Алексей', text: 'проверь деплой' }, 'room-7');
  assert.match(p.content, /Алексей/);
  assert.match(p.content, /проверь деплой/);
  assert.deepEqual(p.meta, { room_id: 'room-7', sender: 'Алексей', msg_id: 'm9' });
  for (const v of Object.values(p.meta)) assert.equal(typeof v, 'string');
  ok('toNotification даёт content + meta из строк');
}

// --- postReply --------------------------------------------------------------
{
  let seenUrl = null, seenOpts = null;
  const fakeFetch = async (url, opts) => { seenUrl = url; seenOpts = opts; return { ok: true, json: async () => ({ ok: true }) }; };
  await postReply(fakeFetch, { api: 'https://x', roomId: 'r2', roomKey: 'k2', fromAgent: 'claude-code', text: 'готово' });
  assert.equal(seenUrl, 'https://x/rooms/r2/post');
  assert.equal(seenOpts.method, 'POST');
  assert.equal(seenOpts.headers['X-Room-Key'], 'k2');
  assert.deepEqual(JSON.parse(seenOpts.body), { from_agent: 'claude-code', text: 'готово' });
  ok('postReply шлёт {from_agent,text} с ключом комнаты');
}
{
  const fakeFetch = async () => ({ ok: false, status: 400, json: async () => ({ error: "field 'text' required" }) });
  await assert.rejects(() => postReply(fakeFetch, { api: 'https://x', roomId: 'r', roomKey: 'k', fromAgent: 'a', text: 'x' }), /HTTP 400/);
  await assert.rejects(() => postReply(fakeFetch, { api: 'https://x', roomId: 'r', roomKey: 'k', fromAgent: 'a', text: '  ' }), /непуст/);
  ok('postReply валидирует пустой текст и поднимает ошибку сервера');
}

console.log(`\n${passed} проверок пройдено`);
