'use strict';

const apiRoot = '/user/memory/records';
const here = new URL(location.href);
const filterNames = ['q', 'domain', 'agent', 'session_id', 'record_id'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const $ = id => document.getElementById(id);
const listURL = new URL('/ui/memory', location.origin);
for (const name of [...filterNames, 'cursor']) {
  if (here.searchParams.get(name)) listURL.searchParams.set(name, here.searchParams.get(name));
}

function node(tag, text, cls) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

const statusLabels = new Map(Object.entries({
  unknown: 'Не указан', pending: 'Ожидает', queued: 'В очереди', active: 'В работе', in_progress: 'В работе',
  running: 'В работе', awaiting_review: 'На проверке', blocked: 'Заблокировано',
  completed: 'Завершено', verified_done: 'Проверено', failed: 'Сбой', cancelled: 'Отменено',
}));
const kindLabels = new Map(Object.entries({
  checkpoint: 'Контрольная точка', session_note: 'Заметка сеанса', memory_document: 'Документ памяти',
  observation: 'Наблюдение', summary: 'Сводка', memory: 'Запись памяти',
}));
function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ru-RU', {dateStyle: 'medium', timeStyle: 'short'});
}

function author(r) {
  return r.agent_label && r.agent_label !== r.source_agent
    ? r.agent_label + (r.source_agent ? ' (' + r.source_agent + ')' : '') : r.source_agent;
}

async function request(path, options) {
  const res = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options});
  if (!res.ok) {
    const error = new Error('HTTP ' + res.status);
    error.status = res.status;
    throw error;
  }
  return res.json();
}

function failure(box, error, retry) {
  box.replaceChildren();
  if (error.status === 401) {
    box.append('Войдите в свой аккаунт и обновите эту страницу. ');
    const link = node('a', 'Войти в новой вкладке');
    link.href = '/ui/login';
    link.target = '_blank';
    link.rel = 'noopener';
    box.append(link);
  } else if (error.status === 403) {
    box.textContent = 'Доступ запрещён. Откройте запись через свой личный кабинет.';
  } else if (error.status === 404) {
    box.textContent = 'Запись недоступна. Проверьте ссылку и аккаунт.';
  } else if (error.status === 422 || error.status === 400) {
    box.textContent = 'Проверьте введённые значения или откройте список с начала.';
  } else {
    box.textContent = 'Не удалось загрузить данные. ';
    if (retry) {
      const button = node('button', 'Повторить');
      button.type = 'button';
      button.onclick = retry;
      box.append(button);
    }
  }
}

function renderRecord(r) {
  const item = node('li', '', 'record');
  const link = node('a', r.task || ('Запись ' + r.id));
  const url = new URL(listURL);
  url.searchParams.set('record', r.id);
  link.href = url;
  const meta = [r.domain, author(r), dateLabel(r.timestamp), statusLabels.get(r.status) || r.status];
  if (r.comment_count) meta.push('Комментарии: ' + r.comment_count);
  item.append(link, node('p', meta.filter(Boolean).join(' · '), 'meta'));
  if (r.result) item.append(node('p', r.result));
  return item;
}

async function loadList() {
  const params = new URLSearchParams(listURL.searchParams);
  $('list-status').textContent = 'Загрузка записей…';
  const first = new URL(listURL);
  first.searchParams.delete('cursor');
  $('first-page').href = first;
  $('first-page').hidden = !params.has('cursor');
  try {
    const page = await request(apiRoot + '?' + params);
    $('records').replaceChildren(...page.items.map(renderRecord));
    $('list-status').textContent = page.items.length ? '' : 'Записей по этому запросу нет.';
    $('next-page').hidden = !page.next_cursor;
    if (page.next_cursor) {
      const next = new URL(listURL);
      next.searchParams.set('cursor', page.next_cursor);
      $('next-page').href = next;
    }
  } catch (e) {
    failure($('list-status'), e, loadList);
  }
}

function setupList() {
  for (const name of filterNames) $('filters').elements.namedItem(name).value = listURL.searchParams.get(name) || '';
  $('filter-options').open = filterNames.slice(1).some(name => listURL.searchParams.has(name));
  $('clear').hidden = !filterNames.some(name => listURL.searchParams.has(name));
  $('filters').onsubmit = e => {
    e.preventDefault();
    const url = new URL('/ui/memory', location.origin);
    for (const [name, value] of new FormData(e.currentTarget)) {
      if (value.trim()) url.searchParams.set(name, value.trim());
    }
    location.assign(url);
  };
  loadList();
}

function showDetail(r) {
  $('record-title').textContent = r.task || 'Запись памяти';
  document.title = $('record-title').textContent + ' — Память проекта';
  const fields = [
    ['Время записи', dateLabel(r.timestamp)], ['Проект', r.domain], ['Автор', author(r)],
    ['Чат / сессия', r.session_id], ['Тип', kindLabels.get(r.kind) || r.kind],
    ['Статус', statusLabels.get(r.status) || r.status], ['UUID', r.id],
  ];
  $('metadata').replaceChildren(...fields.filter(([,v]) => v).flatMap(([k,v]) => [node('dt', k), node('dd', v)]));
  if (r.is_latest === false) {
    const version = node('dd', 'Архивная запись');
    if (uuid.test(r.latest_id)) {
      const current = node('a', 'Открыть актуальную запись');
      current.href = '/ui/memory?record=' + encodeURIComponent(r.latest_id);
      version.append(' · ', current);
    }
    $('metadata').append(node('dt', 'Версия'), version);
  }
  for (const [title, text] of [['Результат', r.result], ['Следующий шаг', r.next_step], ['Уроки', r.lessons]]) {
    if (text) $('body-fields').append(node('h3', title), node('p', text, 'prose'));
  }
  if (r.evidence?.length) {
    const proof = node('ul', '', 'prose');
    for (const source of r.evidence) proof.append(node('li', source));
    $('body-fields').append(node('h3', 'Доказательства'), proof);
  }
  const canonical = new URL('/ui/memory', location.origin);
  canonical.searchParams.set('record', r.id);
  $('permalink').href = canonical;
  $('copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText(canonical.href);
      $('copy-status').textContent = 'Ссылка скопирована.';
    } catch {
      $('copy-status').textContent = 'Скопируйте адрес через меню постоянной ссылки.';
      $('permalink').focus();
    }
  };
  $('detail-status').textContent = '';
  $('detail').hidden = false;
  $('discussion').hidden = false;
}

function setupComments(recordId, path) {
  const comments = new Map();
  let cursor = null;
  function redraw() {
    const items = Array.from(comments.values()).sort((a,b) =>
      Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id)
    );
    $('comments').replaceChildren(...items.map(c => {
      const item = node('li', '', 'comment');
      const time = node('time', dateLabel(c.created_at));
      time.dateTime = c.created_at;
      item.append(time, node('p', c.text, 'prose'));
      return item;
    }));
  }

  async function loadComments() {
    $('more-comments').disabled = true;
    $('comments-status').textContent = 'Загрузка комментариев…';
    try {
      const url = new URL(path, location.origin);
      if (cursor) url.searchParams.set('cursor', cursor);
      const page = await request(url);
      for (const c of page.items) comments.set(c.id, c);
      cursor = page.next_cursor;
      redraw();
      $('more-comments').hidden = !cursor;
      $('comments-status').textContent = comments.size ? '' : 'Комментариев пока нет.';
    } catch (e) {
      failure($('comments-status'), e, loadComments);
      $('more-comments').hidden = true;
    } finally {
      $('more-comments').disabled = false;
    }
  }
  $('more-comments').onclick = loadComments;
  $('compose').onsubmit = async e => {
    e.preventDefault();
    if ($('send').disabled) return;
    const input = $('comment');
    const text = input.value.trim();
    if (!text) {
      $('send-status').textContent = 'Введите текст комментария.';
      input.focus();
      return;
    }
    $('send').disabled = true;
    input.readOnly = true;
    $('send-status').textContent = 'Сохраняем…';
    try {
      // Keep only the digest and retry ID in this history entry, including after reload.
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
      const digest = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
      let pending = history.state?.pendingComment;
      if (!pending || pending.recordId !== recordId || pending.digest !== digest) {
        pending = {recordId, digest, requestId: crypto.randomUUID()};
        history.replaceState({...history.state, pendingComment: pending}, '');
      }
      const c = await request(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text, request_id: pending.requestId})});
      history.replaceState({...history.state, pendingComment: null}, '');
      comments.set(c.id, c);
      redraw();
      if ($('comments-status').textContent === 'Комментариев пока нет.') $('comments-status').textContent = '';
      input.value = '';
      $('send-status').textContent = 'Комментарий сохранён.';
    } catch (e) {
      if (e.status === 401 || e.status === 403 || e.status === 404) failure($('send-status'), e);
      else $('send-status').textContent = 'Не удалось подтвердить сохранение. Повторите отправку.';
    } finally {
      $('send').disabled = false;
      input.readOnly = false;
    }
  };
  loadComments();
}

async function openRecord(id) {
  $('list-view').hidden = true;
  $('detail-view').hidden = false;
  $('back-list').href = listURL;
  if (!uuid.test(id)) {
    $('detail-status').textContent = 'В ссылке указан некорректный UUID записи.';
    return;
  }
  $('detail-status').textContent = 'Загрузка записи…';
  const path = apiRoot + '/' + encodeURIComponent(id);
  try {
    const r = await request(path);
    showDetail(r);
    setupComments(id, path + '/comments');
  } catch (e) {
    failure($('detail-status'), e, () => openRecord(id));
  }
}

if (here.searchParams.has('record')) openRecord(here.searchParams.get('record'));
else setupList();
