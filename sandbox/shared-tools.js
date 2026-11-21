"use strict";

const byId = id => document.getElementById(id);
const endpoint = "/user/shared-tools";
const kinds = {skill: "Навык", script: "Скрипт", prompt: "Промпт", api: "API", library: "Библиотека", service: "Сервис"};
const scopes = {portable: "У любого агента", local: "На определённом ПК", service: "В подключённом сервисе"};
const notes = {recipients: "Кому передан", author_check: "Проверка автора", independent_use: "Независимая проверка"};
let nextOffset = null;
let activeSearch = "";
let generation = 0;
let detailRequest = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function request(url, options) {
  const response = await fetch(url, {credentials: "same-origin", ...options});
  if (response.status === 401) {
    byId("login").hidden = false;
    byId("add").disabled = true;
    byId("list").replaceChildren();
    byId("more").hidden = true;
    throw new Error("Для доступа к каталогу войдите в кабинет.");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : "Не удалось выполнить запрос. Попробуйте ещё раз.");
  }
  return response.json();
}

function labels(item) {
  return [kinds[item.kind] || item.kind, item.domain, scopes[item.scope]].filter(Boolean).join(" · ");
}

function cardButton(item) {
  const card = element("button", "");
  card.type = "button";
  card.append(element("span", labels(item), "meta"), element("strong", item.name, "resource-name"),
              element("span", item.description, "summary"));
  card.addEventListener("click", () => selectCard(item.id));
  return card;
}

async function loadPage(offset = 0) {
  const current = ++generation;
  if (!offset) {
    activeSearch = byId("query").value.trim();
    byId("list").replaceChildren();
    byId("more").hidden = true;
    nextOffset = null;
  }
  byId("more").disabled = true;
  byId("status").textContent = "Загрузка…";
  try {
    const params = new URLSearchParams({q: activeSearch, offset: String(offset)});
    const page = await request(endpoint + "?" + params);
    if (current !== generation) return;
    byId("login").hidden = true;
    byId("add").disabled = false;
    byId("list").append(...page.items.map(cardButton));
    nextOffset = page.next_offset;
    byId("more").hidden = nextOffset === null;
    byId("status").textContent = byId("list").childElementCount ? "" :
      (activeSearch ? "По вашему запросу ничего не найдено." : "В каталоге пока нет ресурсов.");
  } catch (error) {
    if (current === generation) byId("status").textContent = error.message;
  } finally {
    if (current === generation) byId("more").disabled = false;
  }
}

function renderDetail(item) {
  byId("detail-title").textContent = item.name;
  const body = byId("detail-body");
  body.replaceChildren(element("p", labels(item), "meta"), element("p", item.description, "detail-text"));
  if (item.scope === "local") {
    body.append(element("p", "Для запуска нужен доступ к соответствующему ПК и его окружению.", "scope-note"));
  }
  if (item.location) body.append(element("h3", "Путь или ссылка"), element("pre", item.location));
  if (item.instructions) body.append(element("h3", "Инструкция или полный текст"), element("pre", item.instructions));
  for (const [key, value] of Object.entries(item.notes)) {
    if (value && notes[key]) body.append(element("h3", notes[key]), element("p", value, "detail-text"));
  }
  const link = element("a", "Постоянная ссылка на карточку");
  link.href = cardUrl(item.id);
  body.append(element("h3", "ID для агента"), element("pre", item.id), link);
}

function cardUrl(id) {
  const url = new URL(location.href);
  url.pathname = "/ui/shared-tools";
  url.hash = "";
  if (id) url.searchParams.set("tool", id);
  else url.searchParams.delete("tool");
  return url;
}

function selectCard(id) {
  if (new URL(location.href).searchParams.get("tool") !== id) {
    history.pushState({catalogDetail: true}, "", cardUrl(id));
  }
  return openCard(id);
}

async function openCard(id) {
  const current = ++detailRequest;
  byId("detail-title").textContent = "Карточка ресурса";
  byId("detail-body").textContent = "Загрузка…";
  if (!byId("detail").open) byId("detail").showModal();
  try {
    const item = await request(endpoint + "/" + encodeURIComponent(id));
    if (current === detailRequest) renderDetail(item);
  } catch (error) {
    if (current === detailRequest) byId("detail-body").textContent = error.message;
  }
}

function syncDetail() {
  const id = new URL(location.href).searchParams.get("tool");
  if (id) openCard(id);
  else if (byId("detail").open) byId("detail").close();
}

window.addEventListener("popstate", syncDetail);
byId("detail").addEventListener("close", () => {
  ++detailRequest;
  if (new URL(location.href).searchParams.has("tool")) {
    if (history.state?.catalogDetail) history.back();
    else history.replaceState(null, "", cardUrl(null));
  }
});

byId("find").addEventListener("submit", event => { event.preventDefault(); loadPage(); });
byId("more").addEventListener("click", () => { if (nextOffset !== null) loadPage(nextOffset); });
document.querySelectorAll("[data-close]").forEach(button => {
  button.addEventListener("click", () => button.closest("dialog").close());
});
byId("add").addEventListener("click", () => {
  byId("edit-status").textContent = "";
  byId("edit").showModal();
  byId("editor").elements.name.focus();
});
byId("editor").addEventListener("submit", async event => {
  event.preventDefault();
  if (byId("save").disabled) return;
  const form = event.currentTarget;
  byId("save").disabled = true;
  byId("edit-status").textContent = "Сохраняем…";
  try {
    const body = Object.fromEntries(new FormData(form));
    const result = await request(endpoint, {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify(body)});
    byId("edit").close();
    form.reset();
    byId("query").value = "";
    await loadPage();
    await selectCard(result.item.id);
  } catch (error) {
    byId("edit-status").textContent = error.message;
  } finally {
    byId("save").disabled = false;
  }
});
loadPage();
syncDetail();
