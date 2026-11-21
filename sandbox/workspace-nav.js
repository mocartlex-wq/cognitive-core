"use strict";

// Anchors still lead to the original sections when JavaScript is unavailable.
const rail = document.querySelector(".workspace-rail");
const railToggle = rail.querySelector(".workspace-rail-toggle");
const panelLinks = [...rail.querySelectorAll('a[href^="#"]')];
const panels = [...document.querySelectorAll(".workspace-content [data-panel]")];
const heading = document.getElementById("workspace-title");
const mobileRail = window.matchMedia("(max-width: 760px)");

function openRail(open) {
  rail.dataset.open = String(open);
  railToggle.setAttribute("aria-expanded", String(open));
}

function showPanel(moveFocus) {
  const selected = panelLinks.find(link => link.hash === location.hash) || panelLinks[0];
  const key = selected.hash.slice(1);
  for (const link of panelLinks) {
    if (link === selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of panels) panel.hidden = panel.dataset.panel !== key;
  heading.textContent = key === "overview" ? "Рабочее пространство" : selected.textContent;
  if (key === "account") document.getElementById("accountDetails").open = true;
  openRail(!mobileRail.matches);
  if (moveFocus) {
    heading.focus({preventScroll: true});
    window.scrollTo(0, 0);
  }
}

railToggle.hidden = false;
railToggle.addEventListener("click", () => openRail(rail.dataset.open !== "true"));
rail.addEventListener("click", event => {
  const link = event.target.closest('a[href^="#"]');
  if (link && location.hash === link.hash) showPanel(true);
});
mobileRail.addEventListener("change", () => openRail(!mobileRail.matches));
window.addEventListener("hashchange", () => showPanel(true));
showPanel(false);

async function loadRecentMemory() {
  const list = document.getElementById("workspace-recent");
  const status = message => {
    const row = document.createElement("li");
    row.textContent = message;
    list.replaceChildren(row);
  };
  try {
    const response = await fetch("/user/memory/records?limit=4", {credentials: "same-origin"});
    if (!response.ok) {
      status(response.status === 401 ? "Войдите в кабинет, чтобы увидеть свои записи." : "Не удалось загрузить последние записи.");
      return;
    }
    const {items} = await response.json();
    if (!items.length) {
      status("Здесь появятся записи, которые ваши агенты сохранят в память проекта.");
      return;
    }
    const rows = items.map(record => {
      const row = document.createElement("li");
      const link = document.createElement("a");
      link.href = "/ui/memory?record=" + encodeURIComponent(record.id);
      link.textContent = record.task || "Запись памяти";
      const meta = document.createElement("small");
      meta.textContent = [record.domain, record.agent_label || record.source_agent].filter(Boolean).join(" · ");
      const time = document.createElement("time");
      time.dateTime = record.timestamp;
      time.textContent = new Date(record.timestamp).toLocaleDateString("ru", {day: "numeric", month: "short", year: "numeric"});
      row.append(link, meta, time);
      return row;
    });
    list.replaceChildren(...rows);
  } catch {
    status("Не удалось загрузить последние записи.");
  }
}
loadRecentMemory();
