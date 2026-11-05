#!/usr/bin/env node
// Канал Cognitive Core для Claude Code (research preview «Channels»).
//
// Что делает: держит long-poll по комнатам Cognitive Core и пушит каждое новое
// чужое сообщение в ЖИВУЮ сессию Claude Code уведомлением notifications/claude/channel.
// Обратно — инструмент reply(room_id, text), который постит в ту же комнату.
//
// Запуск: claude --dangerously-load-development-channels server:cogcore
// Подробности и переменные окружения — в README.md рядом.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { parseRooms, waitOnce, selectNew } from './lib/poller.mjs';
import { createClaims } from './lib/claims.mjs';

const API = (process.env.COGCORE_API || 'https://mcp.me-ai.ru').replace(/\/+$/, '');
const SELF = process.env.COGCORE_AGENT_ID || 'claude-code';
const ROOMS = parseRooms(process.env.COGCORE_ROOMS);
const SINCE = Number(process.env.COGCORE_SINCE_SECONDS || 5);
const TIMEOUT = Number(process.env.COGCORE_WAIT_TIMEOUT || 25);
const claims = createClaims({ api: API, self: SELF, apiKey: process.env.COGCORE_API_KEY });

const keyByRoom = new Map(ROOMS.map((r) => [r.roomId, r.roomKey]));

const server = new Server(
  { name: 'cogcore', version: '0.1.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} }, // регистрирует слушателя канала
      tools: {},                              // двусторонний канал: нужен reply
    },
    instructions:
      'События приходят из комнат Cognitive Core как <channel source="cogcore">. '
      + 'Это сообщения от владельца и других агентов. Если сообщение обращено к тебе '
      + 'или требует действия — ответь инструментом reply, указав room_id из meta той же '
      + 'комнаты. Отвечай по-русски, коротко и по делу. Если ответ не нужен — просто учти '
      + 'событие и продолжай текущую работу. При reply передавай message_id из meta.msg_id. '
      + 'claimed="false" с claim_status="conflict" означает, что ответ взяла другая сессия: '
      + 'учти событие, но не дублируй ответ. claim_status="unknown" не подтверждает владение. '
      + 'claim_scope="agent" означает изолированный лок, не защиту от других агентов.',
  },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description:
        'Ответить в комнату Cognitive Core. room_id брать из meta пришедшего события '
        + '(отвечать нужно в ТУ ЖЕ комнату, откуда пришло сообщение).',
      inputSchema: {
        type: 'object',
        properties: {
          room_id: { type: 'string', description: 'UUID комнаты из meta.room_id' },
          text: { type: 'string', description: 'Текст ответа, по-русски' },
          message_id: { type: 'string', maxLength: 100, description: 'meta.msg_id исходного сообщения для квитанции done (необязательно)' },
        },
        required: ['room_id', 'text'],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  if (name !== 'reply') {
    return { isError: true, content: [{ type: 'text', text: `Неизвестный инструмент: ${name}` }] };
  }
  const roomId = String((args && args.room_id) || '');
  const text = String((args && args.text) || '');
  const roomKey = keyByRoom.get(roomId);
  if (!roomKey) {
    return {
      isError: true,
      content: [{ type: 'text', text: `Комната ${roomId} не настроена в COGCORE_ROOMS` }],
    };
  }
  try {
    const { receipt } = await claims.reply(
      { api: API, roomId, roomKey, fromAgent: SELF, text },
      args?.message_id ? String(args.message_id) : undefined,
    );
    const receiptNote = receipt === 'skipped' ? '' : `; receipt: ${receipt}`;
    return { content: [{ type: 'text', text: `Отправлено в комнату ${roomId}${receiptNote}` }] };
  } catch (e) {
    return { isError: true, content: [{ type: 'text', text: `Не отправилось: ${e.message}` }] };
  }
});

/** Бесконечный long-poll одной комнаты с бэкоффом на ошибках. */
async function pump({ roomId, roomKey }) {
  const seen = new Set();
  let backoff = 1000;
  for (;;) {
    try {
      const msgs = await waitOnce(fetch, { api: API, roomId, roomKey, sinceSeconds: SINCE, timeout: TIMEOUT });
      backoff = 1000;
      for (const m of selectNew(msgs, { selfAgentId: SELF, seen })) {
        await server.notification({
          method: 'notifications/claude/channel',
          params: await claims.notification(m, roomId),
        });
      }
    } catch (e) {
      console.error(`[cogcore] ${roomId}: ${e.message}; повтор через ${backoff}мс`);
      await new Promise((r) => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 30000);
    }
  }
}

const transport = new StdioServerTransport();
await server.connect(transport);

if (!ROOMS.length) {
  console.error('[cogcore] COGCORE_ROOMS пуст — канал подключён, но слушать нечего. Формат: id:key,id2:key2');
} else {
  console.error(`[cogcore] слушаю комнат: ${ROOMS.length}, агент ${SELF}, API ${API}`);
  for (const r of ROOMS) pump(r);
}
