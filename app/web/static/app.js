"use strict";
/* CampusMind web app: vanilla JS, hash router. All user data is inserted with
   textContent (never as HTML). Views: Today (home), Ask, Search, Calendar,
   Spaces, Space, Sources. */

// ---------- tiny DOM helpers ----------
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/><path d="M10 20v-5h4v5"/>',
  ask: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M12 8.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/>',
  cal: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  layers: '<path d="M12 3 3 7.5l9 4.5 9-4.5z"/><path d="m3 12 9 4.5 9-4.5"/><path d="m3 16.5 9 4.5 9-4.5"/>',
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  chat: '<path d="M4 20l1.3-4A8 8 0 1 1 8 18.7z"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m4 7 8 6 8-6"/>',
  send: '<path d="M21 4 3 11l6.5 2.5L12 20l3-5 6-11z"/><path d="m9.5 13.5 5-4"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  warn: '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>',
  bell: '<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  exam: '<path d="M3 9l9-5 9 5-9 5z"/><path d="M7 11.5V16c3 2 7 2 10 0v-4.5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  star: '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
  pin: '<path d="M9 4h6l-1 6 3 3H7l3-3z"/><path d="M12 13v7"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>',
  snooze: '<path d="M4 9h5l-5 6h5M13 5h4l-4 5h4"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  move: '<path d="M5 12h14M13 6l6 6-6 6"/>',
};
function icon(name, size) {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("class", "i"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true");
  if (size) { s.style.width = size + "px"; s.style.height = size + "px"; }
  s.innerHTML = ICONS[name] || ""; // static, authored markup only
  return s;
}

// ---------- state + api ----------
const S = { me: null, spaces: [], mode: "hybrid", todayCounts: null, seenThisVisit: false, cal: null, timer: null };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts,
    headers: { "X-CampusMind": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
    body: opts.json ? JSON.stringify(opts.json) : opts.body });
  if (r.status === 401) { showLogin(); throw new Error("Not logged in"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : (j.detail?.[0]?.msg || msg); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 3200);
}
const fail = (e) => toast(e.message || String(e));

// ---------- formatting ----------
const tz = () => S.me?.timezone || "Asia/Kolkata";
const F = (iso, o) => new Intl.DateTimeFormat("en-IN", { timeZone: tz(), ...o }).format(new Date(iso));
const fDay = (iso) => F(iso, { weekday: "short", day: "numeric", month: "short" });
const fTime = (iso) => F(iso, { hour: "numeric", minute: "2-digit" }).replace(" am", " AM").replace(" pm", " PM");
const fWhen = (e) => fDay(e.due_at) + (e.all_day ? "" : ", " + fTime(e.due_at));
const fFull = (iso) => F(iso, { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
function countdown(sec) {
  if (sec < 0) return "overdue";
  const m = Math.floor(sec / 60); if (m < 60) return `in ${m}m`;
  const hh = Math.floor(m / 60); if (hh < 48) return `in ${hh}h`;
  return `in ${Math.floor(hh / 24)}d`;
}
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return "just now"; if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`; return fDay(iso);
}
const localDate = (iso) => F(iso, { year: "numeric", month: "2-digit", day: "2-digit" }).split("/").reverse().join("-");
const localHM = (iso) => F(iso, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

const SRC = {
  whatsapp: { icon: "chat", label: "WhatsApp", color: "var(--p-teal)" },
  gmail: { icon: "mail", label: "Gmail", color: "var(--p-violet)" },
  telegram: { icon: "send", label: "Telegram", color: "var(--p-lime)" },
  upload: { icon: "file", label: "PDF / upload", color: "var(--p-amber)" },
};
const TYPE = { exam: { cls: "red", icon: "exam", color: "var(--p-coral)", label: "Exam" },
  deadline: { cls: "orange", icon: "clock", color: "var(--p-amber)", label: "Deadline" },
  event: { cls: "teal", icon: "star", color: "var(--p-teal)", label: "Event" } };
const CAT = { exam: "red", deadline: "orange", event: "teal", notice: "violet", resource: "green", placement: "amber", general: "grey" };
const PASTELS = ["#A7DBD7", "#B9A7EC", "#E7B06E", "#E98A7F", "#D4E77C", "#94D9A0", "#F5C451"];
const spaceColor = (sp, i = 0) => sp.color || PASTELS[(sp.id ?? i) % PASTELS.length];

function srcLine(type, locator = {}, title) {
  const m = SRC[type] || SRC.upload;
  const parts = [m.label];
  if (type === "whatsapp") parts.push(locator.chat_name);
  if (type === "gmail") parts.push((locator.from || "").replace(/<.*>/, "").trim() || locator.from);
  if (type === "telegram") parts.push(locator.forward_sender ? "fwd " + locator.forward_sender : (locator.file_name || null));
  if (type === "upload") parts[0] = title || locator.file_name || "Upload";
  return h("span", { class: "src" }, icon(m.icon), parts.filter(Boolean).join(" · "));
}

// ---------- overlays ----------
function closeOverlays() { document.querySelectorAll(".overlay,.drawer,.drawer-bg").forEach((n) => n.remove()); }
function modal(content, cls = "") {
  closeOverlays();
  const box = h("div", { class: "dialog " + cls, role: "dialog", "aria-modal": "true" }, content);
  const ov = h("div", { class: "overlay", onclick: (e) => { if (e.target === ov) ov.remove(); } }, box);
  document.body.append(ov);
  setTimeout(() => box.querySelector("input,select,textarea,button")?.focus(), 30);
  return { close: () => ov.remove(), box };
}
function confirmBox({ title, text, items, ok = "Delete", danger = true }) {
  return new Promise((res) => {
    const m = modal([
      h("span", { class: "lbl", style: danger ? "color:var(--red)" : "" }, danger ? "Are you sure?" : "Confirm"),
      h("h2", {}, title), text ? h("p", { class: "muted", style: "margin:0" }, text) : null,
      items?.length ? h("ul", {}, items.map((i) => h("li", {}, i))) : null,
      h("div", { class: "dacts" },
        h("button", { class: "btn", type: "button", onclick: () => { m.close(); res(false); } }, "Cancel"),
        h("button", { class: "btn " + (danger ? "danger" : "primary"), type: "button", onclick: () => { m.close(); res(true); } }, ok)),
    ], danger ? "danger" : "");
  });
}
function drawer(content) {
  closeOverlays();
  const bg = h("div", { class: "drawer-bg", onclick: closeOverlays });
  const d = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true" },
    h("div", { style: "display:flex;justify-content:flex-end;margin:-8px -8px 0 0" },
      h("button", { class: "ibtn", type: "button", "aria-label": "Close", onclick: closeOverlays }, icon("x"))), content);
  document.body.append(bg, d);
  return d;
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeOverlays(); closeMenus(); } });
function closeMenus() { document.querySelectorAll(".menu").forEach((m) => m.remove()); }
document.addEventListener("click", (e) => { if (!e.target.closest(".menu,.menu-trigger")) closeMenus(); });
function menu(anchor, items) {
  closeMenus();
  const m = h("div", { class: "menu", role: "menu" }, items.map((it) => it === "-" ? h("hr") :
    it.label ? h("div", { class: "menu-lbl" }, it.label) :
      h("button", { type: "button", class: it.danger ? "danger" : "", onclick: () => { closeMenus(); it.run(); } }, it.icon ? icon(it.icon, 16) : null, it.text)));
  anchor.parentElement.append(m);
}

// ---------- shell ----------
function showLogin() {
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
}
const NAV = [["today", "Home", "home"], ["ask", "Ask", "ask"], ["calendar", "Calendar", "cal"], ["spaces", "Spaces", "grid"], ["sources", "Sources", "layers"]];
function renderNav(route) {
  const due = S.todayCounts?.due_soon || 0;
  const mk = (key, label, ic, cls) => h("a", { href: "#/" + key, class: (route === key || (key === "spaces" && route === "space")) ? "on" : "", "aria-current": route === key ? "page" : null },
    icon(ic), label, key === "calendar" && due ? h("span", { class: "bd red" }, String(due)) : null);
  $("#nav").replaceChildren(...NAV.filter((n) => n[0] !== "spaces").map((n) => mk(...n)));
  $("#tabs").replaceChildren(...NAV.map((n) => mk(...n)));
}
async function loadSpaces() {
  try { S.spaces = await api("/api/spaces"); } catch { return; }
  const cur = location.hash.match(/^#\/space\/(\d+)/)?.[1];
  $("#space-list").replaceChildren(...S.spaces.map((sp, i) => h("a", { href: `#/space/${sp.id}`, class: String(sp.id) === cur ? "on" : "" },
    h("span", { class: "sq", style: `background:${spaceColor(sp, i)}` }), h("span", { class: "nm" }, sp.name),
    sp.pinned ? h("span", { class: "pin", title: "Pinned" }, icon("pin", 13)) : null,
    h("span", { class: "meta" }, String(sp.item_count)))),
    h("a", { href: "#/spaces" }, icon("grid", 15), h("span", { class: "nm" }, "All spaces")));
}
function renderSideBits() {
  const me = S.me, box = $("#gmail-box");
  box.replaceChildren();
  if (me.gmail_available) {
    box.append(h("a", { class: "btn", href: "/gmail/connect", style: "width:100%;justify-content:flex-start" }, icon("mail"), me.gmail_connected ? "Reconnect Gmail" : "Connect Gmail"),
      h("p", {}, h("span", { class: "dot" + (me.gmail_connected ? "" : " off") }),
        me.gmail_connected ? `Connected · synced ${me.gmail_last_sync ? ago(me.gmail_last_sync) : "soon"}` : "Not connected"));
  } else box.append(h("p", {}, h("span", { class: "dot off" }), "Gmail isn't set up on this server"));
  const bot = me.bot_username ? `https://t.me/${me.bot_username}` : "https://t.me/";
  $("#tg-link").href = bot; $("#login-bot").href = bot;
  $("#avatar").textContent = (me.name || "?").trim()[0]?.toUpperCase() || "?";
  $("#avatar").setAttribute("aria-label", `Account: ${me.name || ""}`);
}

// search modes
const MODES = { hybrid: ["Hybrid", "Recommended", "Blends exact words and meaning, so “DBMS exam” also finds “mid-sem schedule”.", "blue"],
  keyword: ["Keyword", "Fastest", "Exact matches in messages, emails and PDFs. Almost instant.", "grey"],
  ai: ["Ask AI", "Cited", "Get a written answer with [n] citations to your saved messages.", "violet"] };
function setMode(m) { S.mode = m; $("#mode-label").textContent = MODES[m][0]; try { localStorage.setItem("cm-mode", m); } catch {} }
function toggleModePop() {
  const pop = $("#mode-pop"), open = pop.classList.toggle("hidden") === false;
  $("#mode-btn").setAttribute("aria-expanded", String(open));
  if (!open) return;
  pop.replaceChildren(h("header", {}, h("h3", {}, "Search mode"), h("p", {}, "Choose how CampusMind finds things in what you've saved.")),
    h("div", { class: "opts" }, Object.entries(MODES).map(([k, [name, badge, desc, col]]) =>
      h("button", { type: "button", class: "opt" + (S.mode === k ? " on" : ""), "aria-pressed": String(S.mode === k), onclick: () => { setMode(k); pop.classList.add("hidden"); $("#q").focus(); } },
        h("span", { style: "flex:1;min-width:0" }, h("span", { class: "ot" }, name, h("span", { class: "bd " + col }, badge)), h("span", { class: "od" }, desc))))));
}

// ---------- router ----------
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "") || "today";
  const [path, qs] = raw.split("?");
  const parts = path.split("/");
  return { route: parts[0] || "today", id: parts[1], params: new URLSearchParams(qs || "") };
}
async function router() {
  closeOverlays(); closeMenus();
  if (S.cal) { S.cal.destroy(); S.cal = null; }
  const { route, id, params } = parseHash();
  renderNav(route);
  const view = $("#view");
  view.replaceChildren(h("div", { class: "page" }, h("p", { class: "muted" }, "Loading…")));
  const views = { today: viewToday, ask: viewAsk, search: viewSearch, calendar: viewCalendar, spaces: viewSpaces, space: viewSpace, sources: viewSources };
  try { await (views[route] || viewToday)(view, { id, params }); } catch (e) { if (e.message !== "Not logged in") view.replaceChildren(h("div", { class: "page" }, h("div", { class: "soft-empty" }, "⚠ " + e.message))); }
  loadSpaces();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", router);

// ---------- shared pieces ----------
function hero({ badge, badgeCls = "teal", title, text, actions }) {
  return h("section", { class: "hero" }, h("div", {},
    badge ? h("span", { class: "bd " + badgeCls, style: "align-self:flex-start" }, badge) : null,
    h("h1", {}, title), text ? h("p", {}, text) : null), actions ? h("div", { class: "acts" }, actions) : null);
}
function secHead(title, link, linkText = "View all") {
  return h("div", { class: "sec-head" }, h("h2", { class: "lbl" }, title), link ? h("a", { href: link }, linkText) : null);
}

function memoryCard(it, ctx = {}) {
  const segId = it.segment_id;
  const title = it.summary && it.summary.length < 140 && !/^photo from/i.test(it.summary) ? it.summary : null;
  const acts = h("div", { class: "card-acts" },
    h("button", { class: "ibtn sm", type: "button", "aria-label": "Open", title: "Open", onclick: () => openSegment(segId) }, icon("open", 16)),
    h("button", { class: "ibtn sm menu-trigger", type: "button", "aria-label": "More actions", onclick: (e) => itemMenu(e.currentTarget, it, ctx) }, icon("more", 16)));
  const sp = (it.spaces || []).slice(0, 3);
  return h("article", { class: "card" },
    h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:8px" }, srcLine(it.source?.type || it.source_type, it.locator, it.source?.title || it.source_title),
      it.importance === "high" ? h("span", { class: "bd red up" }, "Important") : null),
    it.locator?.sender ? h("span", { style: "font-size:14px;font-weight:700" }, it.locator.sender) : null,
    title ? h("h3", { class: "title" }, title) : null,
    h("p", { class: "body " + (title ? "clamp3" : "clamp6") }, it.text),
    h("div", { class: "tags" }, it.category && it.category !== "general" ? h("span", { class: "bd " + (CAT[it.category] || "grey") }, it.category) : null,
      it.subject ? h("span", { class: "tag" }, it.subject) : null,
      sp.map((s) => h("a", { class: "tag", href: `#/space/${s.id}` }, h("span", { class: "sq", style: `width:10px;height:10px;background:${s.color || "var(--p-teal)"}` }), s.name))),
    h("div", { class: "foot" }, h("span", {}, it.observed_at ? fFull(it.observed_at) : ""), acts));
}
function itemMenu(anchor, it, ctx) {
  const others = S.spaces.filter((s) => s.id !== ctx.spaceId);
  const items = [{ text: "Open original", icon: "open", run: () => openSegment(it.segment_id) }];
  if (ctx.snooze) items.push({ text: "Snooze to tomorrow", icon: "snooze", run: () => snooze("segment", it.segment_id) });
  if (ctx.spaceId) {
    items.push("-", { text: "Remove from this space", icon: "x", danger: true, run: async () => {
      await api(`/api/spaces/${ctx.spaceId}/items/${it.segment_id}`, { method: "DELETE" }).catch(fail); toast("Removed from space"); ctx.refresh?.(); } });
    if (others.length) { items.push({ label: "Move to space" }); others.slice(0, 8).forEach((s) => items.push({ text: s.name, icon: "move", run: async () => {
      try { await api(`/api/spaces/${s.id}/items`, { method: "POST", json: { segment_id: it.segment_id } });
        await api(`/api/spaces/${ctx.spaceId}/items/${it.segment_id}`, { method: "DELETE" }); toast(`Moved to ${s.name}`); ctx.refresh?.(); } catch (e) { fail(e); } } })); }
  } else if (others.length) {
    items.push({ label: "Add to space" });
    others.slice(0, 8).forEach((s) => items.push({ text: s.name, icon: "plus", run: () => api(`/api/spaces/${s.id}/items`, { method: "POST", json: { segment_id: it.segment_id } }).then(() => { toast(`Added to ${s.name}`); loadSpaces(); }).catch(fail) }));
  }
  menu(anchor, items);
}
async function snooze(kind, id) {
  try { await api("/api/today/snooze", { method: "POST", json: { kind, id } }); toast("Snoozed until tomorrow morning"); if (parseHash().route === "today") viewToday($("#view"), {}); } catch (e) { fail(e); }
}

function eventRow(e, { onChange } = {}) {
  const t = TYPE[e.type] || TYPE.event;
  const cls = "ev" + (e.status === "conflict" ? " conflict" : e.due_within_24h ? " urgent" : "") + (e.status === "done" ? " done" : "");
  const space = (e.spaces || []).find((s) => !/^(Exams|Deadlines|Notices|Resources|Placements)$/.test(s.name)) || (e.spaces || [])[0];
  return h("article", { class: cls },
    h("span", { class: "eico", style: `background:${e.status === "conflict" ? "var(--p-yellow)" : t.color}` }, icon(e.status === "conflict" ? "warn" : t.icon)),
    h("div", { class: "emain" },
      h("h3", { class: "etitle" }, e.title, space ? h("span", { class: "bd", style: `background:${space.color || "var(--p-teal)"}` }, space.name) : null,
        e.status === "conflict" ? h("span", { class: "bd amber" }, "Conflicting dates") : null,
        e.replaces?.length ? h("span", { class: "bd violet", title: "A newer message changed this date" }, "Moved from " + fDay(e.replaces[0].due_at)) : null,
        e.low_confidence ? h("span", { class: "bd grey" }, "Unsure date") : null),
      h("button", { class: "esrc", type: "button", onclick: () => openEvent(e.id, onChange) },
        e.source ? [icon(SRC[e.source.type]?.icon || "file", 14), e.source.title || SRC[e.source.type]?.label] : [icon("edit", 14), "Added by you"])),
    h("div", { class: "when" }, h("span", { style: "font-size:14px;font-weight:600" }, `${t.label} · ${e.all_day ? "All day" : fTime(e.due_at)}`),
      h("span", { class: "cd" }, e.status === "done" ? "done" : countdown(e.seconds_until))),
    h("div", { class: "ev-acts" },
      e.reminders_sent?.length ? h("span", { class: "rem", title: "Telegram reminders" }, icon("bell"), "Reminded " + e.reminders_sent.join(", ")) : null,
      e.status !== "done" ? h("button", { class: "btn sm", type: "button", onclick: () => setStatus(e.id, "done", onChange) }, icon("check", 15), "Done") : null,
      h("button", { class: "btn sm ghost", type: "button", onclick: () => openEvent(e.id, onChange) }, "Details")));
}
async function setStatus(id, status, after) {
  try { await api(`/api/events/${id}`, { method: "PATCH", json: { status } }); toast(status === "done" ? "Marked done ✓" : "Updated"); after?.(); } catch (e) { fail(e); }
}

// ---------- event + segment panels ----------
async function openEvent(id, after) {
  let e;
  try { e = await api(`/api/events/${id}`); } catch (err) { return fail(err); }
  const refresh = () => { closeOverlays(); after?.(); };
  const t = TYPE[e.type] || TYPE.event;
  const d = drawer([
    h("div", { class: "bds" }, h("span", { class: "bd up " + t.cls }, t.label),
      e.status === "conflict" ? h("span", { class: "bd amber" }, "Conflict") : null,
      e.status === "done" ? h("span", { class: "bd green" }, "Done") : null,
      e.status === "cancelled" ? h("span", { class: "bd grey" }, "Cancelled") : null,
      e.user_edited ? h("span", { class: "bd grey" }, "Edited by you") : null),
    h("h2", {}, e.title),
    h("dl", { class: "kv" },
      h("dt", {}, "When"), h("dd", {}, fWhen(e) + (e.end_at ? " – " + fTime(e.end_at) : "")),
      e.subject ? [h("dt", {}, "Subject"), h("dd", {}, e.subject)] : null,
      e.location ? [h("dt", {}, "Where"), h("dd", {}, e.location)] : null,
      h("dt", {}, "Countdown"), h("dd", { class: "mono" }, countdown(e.seconds_until)),
      h("dt", {}, "Reminders"), h("dd", {}, e.reminders_sent?.length ? "Sent: " + e.reminders_sent.join(", ") : "24h and 2h before, on Telegram"),
      e.spaces?.length ? [h("dt", {}, "Spaces"), h("dd", { class: "tags" }, e.spaces.map((s) => h("a", { class: "tag", href: `#/space/${s.id}` }, s.name)))] : null),
    e.replaces?.length ? h("div", { class: "card alert" }, h("div", { class: "head" }, icon("refresh"), "Date updated by a newer message"),
      h("div", { class: "inner" }, e.replaces.map((o) => h("div", { class: "att" }, h("span", { class: "grow" }, h("b", {}, "Old date: " + fDay(o.due_at) + (o.all_day ? "" : ", " + fTime(o.due_at))), h("span", { class: "meta", style: "display:block" }, "Kept for reference, no reminders")),
        h("button", { class: "btn sm", type: "button", onclick: async () => { try { await api(`/api/events/${o.id}/restore`, { method: "POST" }); toast("Both dates are back as a conflict"); refresh(); } catch (err) { fail(err); } } }, "Not an update"))))) : null,
    e.status === "conflict" ? h("div", { class: "card alert" }, h("div", { class: "head" }, icon("warn"), `Sources disagree: ${[e, ...e.conflicts_with].map((x) => fDay(x.due_at)).join(" vs ")}`),
      h("div", { class: "inner" }, [e, ...e.conflicts_with].map((x) => h("div", { class: "att" }, icon(SRC[x.source?.type]?.icon || "file"),
        h("span", { class: "grow" }, h("b", { style: "display:block" }, fWhen(x)), h("span", { class: "meta" }, (x.source ? (SRC[x.source.type]?.label + " · " + (x.locator?.sender || x.locator?.from || x.source.title || "")) : "Added by you") + (x.observed_at ? " · " + fDay(x.observed_at) : ""))),
        x.id === e.id ? h("span", { class: "bd lime" }, "This one") : null)),
        h("div", { class: "row" },
          h("button", { class: "btn primary sm", type: "button", onclick: () => resolve(e.id, "update", refresh) }, "This is an update (keep " + fDay(e.due_at) + ")"),
          h("button", { class: "btn sm", type: "button", onclick: () => resolve(e.id, "keep_both", refresh) }, "Keep both")))) : null,
    e.snippet ? h("div", { class: "sec" }, h("h3", { class: "lbl" }, "From the source"),
      h("p", { class: "quote" }, e.snippet), e.segment_id ? h("button", { class: "btn sm", type: "button", onclick: () => openSegment(e.segment_id) }, icon("open", 15), "Open original") : null) : null,
    h("div", { class: "row", style: "margin-top:auto" },
      h("button", { class: "btn", type: "button", onclick: () => editEvent(e, refresh) }, icon("edit", 16), "Edit"),
      e.status !== "done" ? h("button", { class: "btn primary", type: "button", onclick: () => setStatus(e.id, "done", refresh) }, icon("check", 16), "Mark done")
        : h("button", { class: "btn", type: "button", onclick: () => setStatus(e.id, "upcoming", refresh) }, "Not done"),
      e.status !== "cancelled" ? h("button", { class: "btn", type: "button", onclick: () => setStatus(e.id, "cancelled", refresh) }, "Cancel event") : null,
      h("button", { class: "btn danger", type: "button", onclick: async () => {
        if (await confirmBox({ title: `Delete “${e.title}”?`, text: "The event is removed from your calendar and reminders. The message it came from stays saved." })) {
          try { await api(`/api/events/${e.id}`, { method: "DELETE" }); toast("Event deleted"); refresh(); } catch (err) { fail(err); } } } }, icon("trash", 16), "Delete")),
  ]);
  return d;
}
async function resolve(id, action, after) {
  try { await api(`/api/events/${id}/resolve`, { method: "POST", json: { action } }); toast(action === "update" ? "Got it: older date retired" : "Kept both dates"); after?.(); } catch (e) { fail(e); }
}
function eventForm(e, onSubmit, title) {
  const start = e?.due_at;
  const f = h("form", { style: "display:flex;flex-direction:column;gap:14px" },
    h("span", { class: "lbl" }, title), h("h2", {}, e?.id ? "Edit event" : "Add an event"),
    h("label", { class: "field" }, h("span", {}, "Title"), h("input", { class: "input", name: "title", required: true, maxlength: 200, value: e?.title || "" })),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", {}, "Type"), h("select", { class: "input", name: "type" }, ["exam", "deadline", "event"].map((v) => h("option", { value: v, selected: (e?.type || "event") === v }, TYPE[v].label)))),
      h("label", { class: "field" }, h("span", {}, "Subject"), h("input", { class: "input", name: "subject", value: e?.subject || "", placeholder: "e.g. DBMS" }))),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", {}, "Date"), h("input", { class: "input", type: "date", name: "date", required: true, value: e?.date || (start ? localDate(start) : "") })),
      h("label", { class: "field" }, h("span", {}, "Time"), h("input", { class: "input", type: "time", name: "time", value: start && !e.all_day ? localHM(start) : "10:00" })),
      h("label", { class: "field" }, h("span", {}, "Ends (optional)"), h("input", { class: "input", type: "time", name: "end", value: e?.end_at ? localHM(e.end_at) : "" }))),
    h("label", { class: "checks" }, h("label", {}, h("input", { type: "checkbox", name: "all_day", checked: !!e?.all_day }), "All day / no time")),
    h("label", { class: "field" }, h("span", {}, "Location"), h("input", { class: "input", name: "location", value: e?.location || "", placeholder: "e.g. LH-3" })),
    h("div", { class: "dacts" }, h("button", { class: "btn", type: "button", onclick: closeOverlays }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, e?.id ? "Save changes" : "Add event")));
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const d = Object.fromEntries(new FormData(f));
    const allDay = !!d.all_day;
    const body = { title: d.title, type: d.type, subject: d.subject || null, location: d.location || null, all_day: allDay,
      start: allDay ? d.date : `${d.date}T${d.time || "09:00"}:00`, end: !allDay && d.end ? `${d.date}T${d.end}:00` : null };
    f.querySelector("[type=submit]").disabled = true;
    try { await onSubmit(body); } catch (err) { fail(err); f.querySelector("[type=submit]").disabled = false; }
  });
  return f;
}
function editEvent(e, after) {
  modal(eventForm(e, async (body) => { await api(`/api/events/${e.id}`, { method: "PATCH", json: body }); closeOverlays(); toast("Saved. Reminders follow the new date"); after?.(); }, "Calendar"));
}
function addEvent(date, after) {
  modal(eventForm({ date }, async (body) => { await api("/api/events", { method: "POST", json: body }); closeOverlays(); toast("Event added"); after?.(); }, "Quick add"));
}
async function openSegment(id) {
  let s;
  try { s = await api(`/api/segments/${id}`); } catch (e) { return fail(e); }
  api(`/api/segments/${id}/opened`, { method: "POST" }).catch(() => {});
  drawer([
    srcLine(s.source.type, s.locator, s.source.title),
    h("h2", {}, s.summary && !/^photo from/i.test(s.summary) ? s.summary : (s.source.title || "Saved item")),
    h("p", { class: "meta", style: "margin:0" }, s.label),
    h("p", { class: "quote" }, s.text),
    s.events?.length ? h("div", { class: "sec" }, h("h3", { class: "lbl" }, "Events from this"), s.events.map((e) => h("button", { class: "att", type: "button", onclick: () => openEvent(e.id) },
      h("span", { class: "eico", style: `width:30px;height:30px;background:${TYPE[e.type]?.color}` }, icon(TYPE[e.type]?.icon || "star", 15)),
      h("span", { class: "grow" }, h("b", { style: "display:block" }, e.title), h("span", { class: "meta" }, fWhen(e) + (e.status !== "upcoming" ? " · " + e.status : "")))))) : null,
    h("div", { class: "sec" }, h("h3", { class: "lbl" }, "Spaces"),
      h("div", { class: "tags" }, (s.spaces || []).map((sp) => h("a", { class: "tag", href: `#/space/${sp.id}` }, sp.name)),
        h("button", { class: "tag menu-trigger", type: "button", onclick: (e) => itemMenu(e.currentTarget, s, {}) }, icon("plus", 13), "Add to space"))),
  ]);
}

// ---------- Today ----------
async function viewToday(view) {
  const first = !S.seenThisVisit;
  const feed = await api(`/api/today${first ? "?mark_seen=true" : ""}`);
  S.seenThisVisit = true; S.todayCounts = feed.counts; renderNav("today");
  const refresh = () => viewToday(view, {});
  const askForm = h("form", { class: "card", style: "flex-direction:row;align-items:center;gap:10px;padding:10px 10px 10px 16px" },
    icon("ask"), h("label", { for: "ask-inline", class: "sr" }, "Ask anything"),
    h("input", { id: "ask-inline", class: "input", style: "border:0;background:transparent;flex:1", placeholder: "Ask anything… e.g. When is my next exam?" }),
    h("button", { class: "btn primary", type: "submit" }, "Ask"));
  askForm.addEventListener("submit", (e) => { e.preventDefault(); const q = $("#ask-inline").value.trim(); if (q) location.hash = `#/ask?q=${encodeURIComponent(q)}`; });
  const parts = [hero({ badge: F(feed.generated_at, { weekday: "short", day: "numeric", month: "short" }) + " · Today", badgeCls: "blue", title: feed.greeting, text: feed.summary,
    actions: [h("a", { class: "btn", href: $("#tg-link").href, target: "_blank", rel: "noopener" }, icon("send"), "Forward via Telegram"),
      h("button", { class: "ibtn", type: "button", "aria-label": "Refresh", style: "border:1px solid var(--line2)", onclick: refresh }, icon("refresh"))] }), askForm];
  const byKey = Object.fromEntries(feed.sections.map((s) => [s.key, s]));
  if (!feed.sections.length) parts.push(h("div", { class: "soft-empty" }, "Nothing needs you right now. Forward a message, PDF or photo to the Telegram bot and it shows up here."));
  if (byKey.due_soon) parts.push(h("section", { class: "sec" }, secHead("Coming up · next 48 hours", "#/calendar?view=list"),
    h("div", { class: "grid" }, byKey.due_soon.items.map((e) => h("button", { class: "soon" + (e.due_within_24h ? " urgent" : ""), type: "button", onclick: () => openEvent(e.id, refresh) },
      h("span", { style: "flex:1;min-width:0" }, h("span", { class: "bds", style: "margin-bottom:6px" }, h("span", { class: "bd up " + (TYPE[e.type]?.cls || "teal") }, TYPE[e.type]?.label),
        e.status === "conflict" ? h("span", { class: "bd amber" }, "Conflict") : null, e.reminders_sent?.length ? h("span", { class: "bd grey" }, icon("bell", 11), "reminded") : null),
        h("span", { class: "t" }, e.title), h("span", { class: "meta" }, [e.subject, fWhen(e)].filter(Boolean).join(" · "))),
      h("span", { class: "cd" }, countdown(e.seconds_until)))))));
  if (byKey.attention) parts.push(h("section", { class: "sec" }, secHead("Needs your attention"), h("div", { class: "masonry" }, byKey.attention.items.map((a) => attentionCard(a, refresh)))));
  if (byKey.new) parts.push(h("section", { class: "sec" }, secHead(`New since you last looked · ${byKey.new.total}`, "#/sources"),
    h("div", { class: "masonry" }, byKey.new.items.map((it) => memoryCard(it, { snooze: true })))));
  if (byKey.week) parts.push(h("section", { class: "sec" }, secHead("This week", "#/calendar?view=week"),
    byKey.week.items.map((day) => h("section", { class: "group" }, h("h3", { class: "gh" }, h("span", { class: "gdot" }), day.label, h("span", { class: "meta", style: "font-weight:500" }, `${day.events.length} item${day.events.length > 1 ? "s" : ""}`)),
      day.events.map((e) => eventRow(e, { onChange: refresh }))))));
  if (byKey.spaces) parts.push(h("section", { class: "sec" }, secHead("From your spaces", "#/spaces"),
    byKey.spaces.items.map((g) => h("div", { class: "sec" }, h("a", { href: `#/space/${g.space.id}`, style: "display:flex;align-items:center;gap:10px;font-weight:700;text-decoration:none;color:var(--text)" },
      h("span", { class: "sq", style: `background:${g.space.color || "var(--p-teal)"}` }), g.space.name, h("span", { class: "meta" }, `${g.space.item_count} items`)),
      h("div", { class: "masonry" }, g.items.map((it) => memoryCard(it, { snooze: true })))))));
  if (byKey.rediscover) parts.push(h("section", { class: "sec" }, secHead("Rediscover · saved a while ago, never opened"),
    h("div", { class: "masonry" }, byKey.rediscover.items.map((it) => memoryCard(it, { snooze: true })))));
  view.replaceChildren(h("div", { class: "page" }, parts));
}
function attentionCard(a, refresh) {
  if (a.kind === "conflict") {
    const [first] = a.events, newest = [...a.events].sort((x, y) => (y.observed_at || "").localeCompare(x.observed_at || ""))[0];
    return h("article", { class: "card alert" }, h("div", { class: "head" }, icon("warn"), `${a.events.length} sources disagree: ${a.events.map((e) => fDay(e.due_at)).join(" vs ")}`),
      h("div", { class: "inner" }, h("div", { class: "bds" }, h("span", { class: "bd up " + (TYPE[first.type]?.cls || "teal") }, TYPE[first.type]?.label), h("span", { class: "bd amber" }, "Conflict")),
        h("h3", { class: "title" }, a.title),
        a.events.map((e) => h("button", { class: "att", type: "button", onclick: () => openEvent(e.id, refresh) }, icon(SRC[e.source?.type]?.icon || "file"),
          h("span", { class: "grow" }, h("b", { style: "display:block" }, fWhen(e)), h("span", { class: "meta" }, `${SRC[e.source?.type]?.label || "You"} · ${e.locator?.sender || e.locator?.from || e.source?.title || ""} · ${e.observed_at ? fDay(e.observed_at) : ""}`)),
          e === newest ? h("span", { class: "bd blue" }, "Newer") : null)),
        h("div", { class: "row" }, h("button", { class: "btn primary sm", type: "button", onclick: () => resolve(newest.id, "update", refresh) }, "This is an update"),
          h("button", { class: "btn sm", type: "button", onclick: () => resolve(newest.id, "keep_both", refresh) }, "Keep both"),
          h("button", { class: "btn sm ghost", type: "button", onclick: () => snooze("event", first.id) }, icon("snooze", 15), "Later"))));
  }
  if (a.kind === "low_confidence") {
    const e = a.events[0];
    return h("article", { class: "card alert" }, h("div", { class: "head" }, icon("warn"), "Is this date right?"),
      h("div", { class: "inner" }, h("h3", { class: "title" }, e.title), h("p", { class: "body" }, `I read it as ${fWhen(e)}${e.date_text ? ` from “${e.date_text}”` : ""}.`),
        h("div", { class: "row" }, h("button", { class: "btn primary sm", type: "button", onclick: () => api(`/api/events/${e.id}`, { method: "PATCH", json: { status: "upcoming" } }).then(() => { toast("Confirmed ✓"); refresh(); }).catch(fail) }, "Looks right"),
          h("button", { class: "btn sm", type: "button", onclick: () => editEvent(e, refresh) }, icon("edit", 15), "Fix date"))));
  }
  const j = a.job;
  return h("article", { class: "card alert red" }, h("div", { class: "head" }, icon("warn"), "Couldn't read this file"),
    h("div", { class: "inner" }, h("h3", { class: "title" }, a.title), h("p", { class: "meta", style: "margin:0" }, j.last_error || "Processing failed"),
      h("div", { class: "row" }, h("a", { class: "btn sm", href: "#/sources" }, "Open Sources"))));
}

// ---------- Ask ----------
async function viewAsk(view, { params }) {
  const q = params.get("q") || "";
  if (!q) {
    const f = h("form", { class: "card", style: "gap:14px" }, h("label", { class: "lbl", for: "ask-big" }, "Ask your saved info"),
      h("textarea", { id: "ask-big", class: "input", rows: 3, placeholder: "e.g. When is the DBMS midsem now?" }), h("div", { class: "dacts" }, h("button", { class: "btn primary", type: "submit" }, icon("ask"), "Ask")));
    f.addEventListener("submit", (e) => { e.preventDefault(); const v = $("#ask-big").value.trim(); if (v) location.hash = `#/ask?q=${encodeURIComponent(v)}`; });
    view.replaceChildren(h("div", { class: "page" }, hero({ badge: "Answers with citations", badgeCls: "violet", title: "Ask", text: "Every claim links to the message, email or PDF it came from." }), f,
      h("div", { class: "grid" }, ["When is my next exam?", "What's due this week?", "Did any exam date change?", "What's the fee deadline?"].map((s, i) =>
        h("a", { class: "sug", href: `#/ask?q=${encodeURIComponent(s)}`, style: `background:${PASTELS[i]}` }, h("span", { class: "st" }, "Try"), h("span", { class: "sb" }, s))))));
    return;
  }
  $("#q").value = q;
  view.replaceChildren(h("div", { class: "page" }, h("section", { class: "hero" }, h("div", {}, h("span", { class: "lbl", style: "color:var(--violet)" }, "You asked"), h("h1", {}, q))),
    h("div", { class: "card" }, h("p", { class: "muted", style: "margin:0" }, "Reading your saved messages…"))));
  const r = await api("/api/ask", { method: "POST", json: { question: q } });
  const notFound = !r.citations.length && /couldn't find/i.test(r.answer);
  const head = h("section", { class: "hero" }, h("div", {}, h("span", { class: "lbl", style: "color:var(--violet)" }, "You asked"), h("h1", {}, q)));
  if (notFound) {
    view.replaceChildren(h("div", { class: "page" }, head, h("section", { class: "empty" }, h("span", { class: "ic" }, icon("search", 26)),
      h("h2", {}, "I couldn't find that in your saved info"), h("p", {}, "Nothing you've saved answers this yet. It might be in a message you haven't forwarded.")),
      h("section", { class: "sec" }, h("h2", { class: "lbl" }, "Try this instead"), h("div", { class: "grid" },
        h("a", { class: "sug", href: `#/search?mode=keyword&q=${encodeURIComponent(q)}`, style: "background:var(--p-teal)" }, h("span", { class: "st" }, "Switch mode"), h("span", { class: "sb" }, "Search the exact words instead")),
        h("a", { class: "sug", href: $("#tg-link").href, target: "_blank", rel: "noopener", style: "background:var(--p-lime)" }, h("span", { class: "st" }, "Save it"), h("span", { class: "sb" }, "Forward the notice to the Telegram bot when it arrives")),
        h("a", { class: "sug", href: "#/calendar?view=list", style: "background:var(--p-violet)" }, h("span", { class: "st" }, "Browse"), h("span", { class: "sb" }, "See everything coming up on your calendar"))))));
    return;
  }
  const ans = h("p", { class: "answer" });
  for (const part of r.answer.split(/(\[\d+\])/)) {
    const m = part.match(/^\[(\d+)\]$/);
    ans.append(m ? h("a", { class: "cite", href: `#cite-${m[1]}`, onclick: (e) => { e.preventDefault(); const t = document.getElementById(`cite-${m[1]}`); t?.scrollIntoView({ behavior: "smooth", block: "center" }); t?.classList.add("flash"); setTimeout(() => t?.classList.remove("flash"), 1400); } }, m[1]) : part);
  }
  const cited = r.citations.length ? r.citations : r.context.slice(0, 3);
  view.replaceChildren(h("div", { class: "page" }, head,
    h("section", { class: "card", style: "padding:24px" }, h("div", { class: "bds" }, h("span", { class: "bd violet" }, "Answer"), h("span", { class: "meta" }, `${r.citations.length} source${r.citations.length === 1 ? "" : "s"} cited`)), ans),
    h("section", { class: "sec" }, h("h2", { class: "lbl" }, "Sources"),
      cited.map((c, i) => h("article", { class: "card cite-card", id: `cite-${c.n}` },
        h("div", { style: "display:flex;align-items:center;gap:12px" }, h("span", { class: "num", style: `background:${SRC[c.source_type]?.color || PASTELS[i]}` }, String(c.n)),
          h("span", { style: "flex:1;min-width:0" }, h("b", { style: "display:block" }, c.label)),
          c.segment_id ? h("button", { class: "btn sm", type: "button", onclick: () => openSegment(c.segment_id) }, icon("open", 15), "Open original") : null),
        h("p", { class: "quote clamp6" }, c.snippet))))));
}

// ---------- Search ----------
async function viewSearch(view, { params }) {
  const q = params.get("q") || "", mode = params.get("mode") || "hybrid";
  $("#q").value = q;
  const res = await api(`/api/search?k=20&mode=${mode}&q=${encodeURIComponent(q)}`);
  view.replaceChildren(h("div", { class: "page" },
    hero({ badge: `${MODES[mode][0]} search · ${res.length} result${res.length === 1 ? "" : "s"}`, badgeCls: mode === "keyword" ? "grey" : "blue", title: q,
      actions: [h("a", { class: "btn primary", href: `#/ask?q=${encodeURIComponent(q)}` }, icon("ask"), "Ask AI instead"),
        h("a", { class: "btn", href: `#/search?mode=${mode === "keyword" ? "hybrid" : "keyword"}&q=${encodeURIComponent(q)}` }, mode === "keyword" ? "Try Hybrid" : "Exact words only")] }),
    res.length ? h("div", { class: "masonry" }, res.map((r) => memoryCard({ ...r, source_type: r.source.type }))) : h("div", { class: "soft-empty" }, "No matches. Try different words, or Ask AI.")));
}

// ---------- Calendar ----------
async function viewCalendar(view, { params }) {
  const mobile = matchMedia("(max-width: 900px)").matches;
  const wanted = params.get("view");
  const initial = wanted === "list" || (mobile && !wanted) ? "listMonth" : wanted === "week" ? "timeGridWeek" : "dayGridMonth";
  const week = await api("/api/events?days=7");
  const now = Date.now();
  const filt = { space: params.get("space") || "", types: new Set() };
  const spaceSel = h("select", { class: "input", style: "width:auto;min-width:180px", "aria-label": "Filter by space" }, h("option", { value: "" }, "All spaces"),
    S.spaces.map((s) => h("option", { value: s.id, selected: String(s.id) === filt.space }, s.name)));
  const typeChips = ["exam", "deadline", "event"].map((t) => h("button", { class: "chip", type: "button", "aria-pressed": "false", onclick: (e) => {
    filt.types.has(t) ? filt.types.delete(t) : filt.types.add(t); e.currentTarget.classList.toggle("on"); e.currentTarget.setAttribute("aria-pressed", String(filt.types.has(t))); S.cal?.refetchEvents(); } },
    h("span", { class: "sq", style: `width:10px;height:10px;background:${TYPE[t].color}` }), TYPE[t].label + "s"));
  spaceSel.addEventListener("change", () => { filt.space = spaceSel.value; S.cal?.refetchEvents(); });
  const calEl = h("div", {});
  view.replaceChildren(h("div", { class: "page" },
    hero({ badge: `${F(new Date().toISOString(), { weekday: "short", day: "numeric", month: "short" })} · ${week.filter((e) => e.status !== "done").length} this week`, badgeCls: "teal", title: "Calendar",
      text: "Every exam, deadline and event pulled from your messages, with Telegram reminders 24h and 2h before.",
      actions: [h("button", { class: "btn", type: "button", onclick: subscribeDialog }, icon("link"), "Subscribe in Google Calendar"),
        h("button", { class: "btn primary", type: "button", onclick: () => addEvent(localDate(new Date().toISOString()), () => S.cal?.refetchEvents()) }, icon("plus"), "Add event")] }),
    h("div", { class: "stats" },
      h("button", { class: "stat", type: "button", style: "background:var(--p-coral)", onclick: () => S.cal?.changeView("listWeek") }, h("span", { class: "sn" }, String(week.filter((e) => e.due_within_24h && e.status !== "done").length)), h("span", { class: "sl" }, "Due within 24h")),
      h("button", { class: "stat", type: "button", style: "background:var(--p-lime)", onclick: () => S.cal?.changeView("listWeek") }, h("span", { class: "sn" }, String(week.filter((e) => e.status === "upcoming").length)), h("span", { class: "sl" }, "Coming up · 7 days")),
      h("button", { class: "stat", type: "button", style: "background:var(--p-yellow)", onclick: () => S.cal?.changeView("listMonth") }, h("span", { class: "sn" }, String(week.filter((e) => e.status === "conflict").length)), h("span", { class: "sl" }, "Conflicts to resolve")),
      h("button", { class: "stat", type: "button", style: "background:var(--p-green)" }, h("span", { class: "sn" }, String(week.filter((e) => e.status === "done").length)), h("span", { class: "sl" }, "Done this week"))),
    h("div", { class: "chips" }, spaceSel, h("span", { class: "chip-sep" }), typeChips),
    h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:var(--p-coral)" }), "Exam"), h("span", {}, h("i", { style: "background:var(--p-amber)" }), "Deadline"),
      h("span", {}, h("i", { style: "background:var(--p-teal)" }), "Event"), h("span", {}, h("i", { class: "ring" }), "Due within 24h"),
      h("span", {}, h("i", { class: "dash" }), "Conflicting dates"), h("span", { style: "opacity:.5;text-decoration:line-through" }, "Done")),
    h("div", { class: "cal-wrap" }, calEl)));
  await waitFor(() => window.FullCalendar);
  const refetch = () => S.cal?.refetchEvents();
  S.cal = new FullCalendar.Calendar(calEl, {
    initialView: initial, height: "auto", firstDay: 1, nowIndicator: true, dayMaxEvents: 3,
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listMonth" },
    buttonText: { today: "Today", month: "Month", week: "Week", list: "List" },
    titleFormat: { month: "long", year: "numeric" }, dayHeaderFormat: { weekday: "short" },
    eventTimeFormat: { hour: "numeric", minute: "2-digit", meridiem: "short" },
    listDayFormat: { weekday: "long" }, listDaySideFormat: { day: "numeric", month: "short", year: "numeric" },
    noEventsText: "Nothing here. Click a day (or Add event) to add one.",
    events: async (info, ok, bad) => {
      try {
        const qs = new URLSearchParams({ start: info.startStr, end: info.endStr });
        if (filt.space) qs.set("space_id", filt.space);
        filt.types.forEach((t) => qs.append("type", t));
        const evs = await api(`/api/events?${qs}`);
        ok(evs.map((e) => ({ id: String(e.id), title: (e.status === "conflict" ? "⚠ " : "") + e.title,
          start: e.all_day ? localDate(e.due_at) : e.due_at, end: e.end_at && !e.all_day ? e.end_at : undefined, allDay: !!e.all_day,
          classNames: ["cm-" + e.type, e.due_within_24h && e.status !== "done" ? "cm-due24" : "", e.status === "conflict" ? "cm-conflict" : "", e.status === "done" || e.status === "past" ? "cm-done" : ""].filter(Boolean),
          extendedProps: e })));
      } catch (err) { bad(err); fail(err); }
    },
    eventClick: (info) => { info.jsEvent.preventDefault(); openEvent(Number(info.event.id), refetch); },
    dateClick: (info) => addEvent(info.dateStr.slice(0, 10), refetch),
  });
  S.cal.render();
}
function waitFor(test, ms = 8000) {
  return new Promise((res, rej) => { const t0 = Date.now(); (function poll() { if (test()) return res(); if (Date.now() - t0 > ms) return rej(new Error("Calendar library didn't load (offline?)")); setTimeout(poll, 60); })(); });
}
async function subscribeDialog() {
  let sub;
  try { sub = await api("/api/calendar/subscription"); } catch (e) { return fail(e); }
  const input = h("input", { class: "input mono", readonly: true, value: sub.url, onclick: (e) => e.target.select() });
  const m = modal([h("span", { class: "lbl" }, "Calendar feed"), h("h2", {}, "Subscribe in Google Calendar"),
    h("p", { class: "muted", style: "margin:0" }, "Google Calendar → Other calendars → + → From URL, then paste this link. It updates by itself (Google refreshes every few hours). Keep it private: anyone with the link can see your events."),
    input,
    h("div", { class: "dacts" },
      h("button", { class: "btn", type: "button", onclick: async () => {
        if (await confirmBox({ title: "Make a new link?", text: "The current link stops working. You'll need to subscribe again with the new one.", ok: "New link" })) {
          try { const n = await api("/api/calendar/subscription/regenerate", { method: "POST" }); subscribeDialog(); toast("New private link created"); } catch (e) { fail(e); } } } }, icon("refresh", 16), "New link"),
      h("a", { class: "btn", href: sub.webcal }, "Open in calendar app"),
      h("button", { class: "btn primary", type: "button", onclick: async () => { try { await navigator.clipboard.writeText(sub.url); toast("Link copied"); } catch { input.select(); toast("Press Ctrl+C to copy"); } } }, "Copy link"))]);
}

// ---------- Spaces ----------
async function viewSpaces(view) {
  await loadSpaces();
  view.replaceChildren(h("div", { class: "page" },
    hero({ badge: `${S.spaces.length} spaces`, badgeCls: "teal", title: "Spaces", text: "Your saved items, organized automatically by subject and type. Make your own with rules.",
      actions: [h("button", { class: "btn primary", type: "button", onclick: () => spaceDialog() }, icon("plus"), "New space")] }),
    h("div", { class: "grid" }, S.spaces.map((sp, i) => h("a", { class: "space-card", href: `#/space/${sp.id}` },
      h("div", { class: "top-row" }, h("span", { class: "space-ic", style: `width:40px;height:40px;font-size:18px;background:${spaceColor(sp, i)}` }, sp.icon || sp.name[0]),
        h("span", { class: "nm" }, sp.name), sp.pinned ? icon("pin", 15) : null),
      h("div", { class: "bds" }, h("span", { class: "bd grey" }, `${sp.item_count} items`), h("span", { class: "bd " + (sp.kind === "custom" ? "violet" : "teal") }, sp.kind === "custom" ? "Rules" : "Automatic")),
      sp.next_event ? h("span", { class: "meta" }, `Next: ${sp.next_event.title} · ${fDay(sp.next_event.due_at)}`) : h("span", { class: "meta" }, "No upcoming events"))))));
}
async function viewSpace(view, { id, params }) {
  await loadSpaces();
  const sp = S.spaces.find((s) => String(s.id) === id);
  if (!sp) { view.replaceChildren(h("div", { class: "page" }, h("div", { class: "soft-empty" }, "This space doesn't exist any more."))); return; }
  const st = { type: params.get("type") || "", source: params.get("source") || "", cursor: null, q: "" };
  const grid = h("div", { class: "masonry" }), more = h("button", { class: "btn hidden", type: "button" }, "Load more");
  const refresh = () => viewSpace(view, { id, params });
  async function load(reset) {
    if (reset) { grid.replaceChildren(); st.cursor = null; }
    if (st.q) {
      const res = await api(`/api/search?k=30&space_id=${sp.id}&q=${encodeURIComponent(st.q)}`);
      grid.replaceChildren(...(res.length ? res.map((r) => memoryCard({ ...r, source_type: r.source.type }, { spaceId: sp.id, refresh })) : [h("div", { class: "soft-empty" }, "No matches in this space.")]));
      more.classList.add("hidden"); return;
    }
    const qs = new URLSearchParams({ limit: "24" });
    if (st.cursor) qs.set("cursor", st.cursor); if (st.type) qs.set("type", st.type); if (st.source) qs.set("source", st.source);
    const page = await api(`/api/spaces/${sp.id}/items?${qs}`);
    grid.append(...page.items.map((it) => memoryCard(it, { spaceId: sp.id, refresh })));
    if (!grid.children.length) grid.append(h("div", { class: "soft-empty" }, "Nothing here yet."));
    st.cursor = page.next_cursor; more.classList.toggle("hidden", !page.next_cursor);
  }
  more.addEventListener("click", () => load(false).catch(fail));
  const chip = (label, key, val) => h("button", { class: "chip" + (st[key] === val ? " on" : ""), type: "button", onclick: (e) => {
    st[key] = st[key] === val ? "" : val; e.currentTarget.parentElement.querySelectorAll(`[data-k=${key}]`).forEach((c) => c.classList.toggle("on", c.dataset.v === st[key]));
    load(true).catch(fail); }, "data-k": key, "data-v": val }, label);
  const sInput = h("input", { class: "input", type: "search", placeholder: `Search in ${sp.name}…`, style: "max-width:360px" });
  let deb; sInput.addEventListener("input", () => { clearTimeout(deb); deb = setTimeout(() => { st.q = sInput.value.trim(); load(true).catch(fail); }, 300); });
  const upcoming = await api(`/api/events?days=60&space_id=${sp.id}`).catch(() => []);
  const actMenu = h("button", { class: "btn menu-trigger", type: "button", onclick: (e) => menu(e.currentTarget, [
    { text: sp.pinned ? "Unpin" : "Pin to top", icon: "pin", run: () => api(`/api/spaces/${sp.id}`, { method: "PATCH", json: { pinned: !sp.pinned } }).then(refresh).catch(fail) },
    { text: "Rename", icon: "edit", run: () => renameSpace(sp, refresh) },
    { text: sp.kind === "custom" ? "Edit rules" : "Add rules", icon: "grid", run: () => spaceDialog(sp) },
    { text: "Merge into…", icon: "move", run: () => mergeSpace(sp) }, "-",
    { text: "Delete space", icon: "trash", danger: true, run: async () => {
      if (await confirmBox({ title: `Delete “${sp.name}”?`, text: "Only the space goes. Every message, email and PDF in it stays saved.", ok: "Delete space" })) {
        try { await api(`/api/spaces/${sp.id}`, { method: "DELETE" }); toast("Space deleted"); location.hash = "#/spaces"; } catch (e) { fail(e); } } } }]) }, icon("more"), "Space");
  const r = sp.rules || {};
  const ruleBits = [r.any_keywords?.length && `any of: ${r.any_keywords.join(", ")}`, r.all_keywords?.length && `all of: ${r.all_keywords.join(", ")}`,
    r.senders?.length && `from ${r.senders.join(", ")}`, r.source_types?.length && `via ${r.source_types.join(", ")}`, r.categories?.length && `type ${r.categories.join(", ")}`,
    r.similar_to && `similar to “${r.similar_to}”`].filter(Boolean);
  view.replaceChildren(h("div", { class: "page" },
    h("section", { class: "hero" }, h("div", {}, h("div", { class: "space-title" }, h("span", { class: "space-ic", style: `background:${spaceColor(sp)}` }, sp.icon || sp.name[0]),
      h("div", {}, h("span", { class: "bd " + (sp.kind === "custom" ? "violet" : "teal") }, sp.kind === "custom" ? "Custom space" : "Automatic space"), h("h1", { style: "margin-top:6px" }, sp.name))),
      h("p", {}, `${sp.item_count} items${ruleBits.length ? " · rules: " + ruleBits.join(" · ") : ""}`)),
      h("div", { class: "acts" }, actMenu)),
    upcoming.length ? h("section", { class: "sec" }, secHead(`Upcoming in ${sp.name}`, `#/calendar?space=${sp.id}`, "Open calendar"), upcoming.slice(0, 5).map((e) => eventRow(e, { onChange: refresh }))) : null,
    h("div", { class: "chips" }, chip("Exams", "type", "exam"), chip("Deadlines", "type", "deadline"), chip("Notices", "type", "notice"), chip("Resources", "type", "resource"), chip("Placements", "type", "placement"),
      h("span", { class: "chip-sep" }), chip("WhatsApp", "source", "whatsapp"), chip("Gmail", "source", "gmail"), chip("Telegram", "source", "telegram"), chip("Uploads", "source", "upload")),
    sInput, grid, more));
  await load(true);
}
function renameSpace(sp, after) {
  const input = h("input", { class: "input", value: sp.name, maxlength: 60, required: true });
  const f = h("form", { style: "display:flex;flex-direction:column;gap:14px" }, h("h2", {}, "Rename space"), input,
    h("div", { class: "dacts" }, h("button", { class: "btn", type: "button", onclick: closeOverlays }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, "Rename")));
  f.addEventListener("submit", async (e) => { e.preventDefault(); try { await api(`/api/spaces/${sp.id}`, { method: "PATCH", json: { name: input.value.trim() } }); closeOverlays(); toast("Renamed"); after(); } catch (err) { fail(err); } });
  modal(f);
}
function mergeSpace(sp) {
  const sel = h("select", { class: "input" }, S.spaces.filter((s) => s.id !== sp.id).map((s) => h("option", { value: s.id }, `${s.name} (${s.item_count})`)));
  const f = h("form", { style: "display:flex;flex-direction:column;gap:14px" }, h("span", { class: "lbl" }, "Merge"), h("h2", {}, `Merge “${sp.name}” into…`),
    h("p", { class: "muted", style: "margin:0" }, `All ${sp.item_count} items move over and “${sp.name}” disappears. New items for it go to the target too.`), sel,
    h("div", { class: "dacts" }, h("button", { class: "btn", type: "button", onclick: closeOverlays }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, "Merge")));
  f.addEventListener("submit", async (e) => { e.preventDefault(); try { const t = await api(`/api/spaces/${sp.id}/merge`, { method: "POST", json: { into_id: Number(sel.value) } }); closeOverlays(); toast(`Merged into ${t.name}`); location.hash = `#/space/${t.id}`; } catch (err) { fail(err); } });
  modal(f);
}
function spaceDialog(existing) {
  const r = existing?.rules || {};
  const list = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
  let color = existing?.color || PASTELS[S.spaces.length % PASTELS.length];
  const name = h("input", { class: "input", name: "name", required: true, maxlength: 60, value: existing?.name || "", placeholder: "e.g. Placements" });
  const fld = (label, nameAttr, val, ph, hint) => h("label", { class: "field" }, h("span", {}, label), h("input", { class: "input", name: nameAttr, value: val || "", placeholder: ph }), hint ? h("small", {}, hint) : null);
  const checks = (nameAttr, opts, sel = []) => h("div", { class: "checks" }, opts.map(([v, l]) => h("label", {}, h("input", { type: "checkbox", name: nameAttr, value: v, checked: sel.includes(v) }), l)));
  const swatches = h("div", { class: "chips" }, PASTELS.map((c) => h("button", { type: "button", class: "chip" + (c === color ? " on" : ""), "aria-label": "Color " + c, style: `width:36px;padding:0;justify-content:center;background:${c}`,
    onclick: (e) => { color = c; swatches.querySelectorAll(".chip").forEach((x) => x.classList.remove("on")); e.currentTarget.classList.add("on"); } }, c === color ? icon("check", 14) : "")));
  const preview = h("div", { class: "preview" }, h("span", { class: "meta" }, "Add a rule to see what matches."));
  const f = h("form", { style: "display:flex;flex-direction:column;gap:14px" },
    h("span", { class: "lbl" }, existing ? "Edit space" : "New space"), h("h2", {}, existing ? existing.name : "Create a space"),
    h("label", { class: "field" }, h("span", {}, "Name"), name), h("div", { class: "field" }, h("span", {}, "Color"), swatches),
    h("h3", { class: "lbl", style: "margin-top:6px" }, "Rules: items matching ALL filled rules are added automatically"),
    fld("Any of these words", "any_keywords", (r.any_keywords || []).join(", "), "placement, TCS, internship", "Comma separated"),
    fld("All of these words", "all_keywords", (r.all_keywords || []).join(", "), "e.g. DBMS, lab"),
    fld("From senders or email domains", "senders", (r.senders || []).join(", "), "placement@college.edu.in, @college.edu.in, Riya"),
    h("div", { class: "field" }, h("span", {}, "Came via"), checks("source_types", [["whatsapp", "WhatsApp"], ["gmail", "Gmail"], ["telegram", "Telegram"], ["upload", "Uploads"]], r.source_types)),
    h("div", { class: "field" }, h("span", {}, "Type"), checks("categories", [["exam", "Exams"], ["deadline", "Deadlines"], ["event", "Events"], ["notice", "Notices"], ["resource", "Resources"], ["placement", "Placements"]], r.categories)),
    h("div", { class: "row" }, h("label", { class: "field" }, h("span", {}, "From date"), h("input", { class: "input", type: "date", name: "date_from", value: r.date_from ? r.date_from.slice(0, 10) : "" })),
      h("label", { class: "field" }, h("span", {}, "To date"), h("input", { class: "input", type: "date", name: "date_to", value: r.date_to ? r.date_to.slice(0, 10) : "" }))),
    fld("Similar in meaning to (optional)", "similar_to", r.similar_to, "e.g. campus recruitment drive", "Uses AI meaning search; finds related wording too"),
    preview,
    h("div", { class: "dacts" }, h("button", { class: "btn", type: "button", onclick: closeOverlays }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, existing ? "Save" : "Create space")));
  const rules = () => {
    const d = new FormData(f);
    const out = { any_keywords: list(d.get("any_keywords")), all_keywords: list(d.get("all_keywords")), senders: list(d.get("senders")),
      source_types: d.getAll("source_types"), categories: d.getAll("categories") };
    if (d.get("date_from")) out.date_from = d.get("date_from") + "T00:00:00+05:30";
    if (d.get("date_to")) out.date_to = d.get("date_to") + "T23:59:59+05:30";
    if ((d.get("similar_to") || "").trim()) out.similar_to = d.get("similar_to").trim();
    return out;
  };
  const empty = (o) => !o.any_keywords.length && !o.all_keywords.length && !o.senders.length && !o.source_types.length && !o.categories.length && !o.date_from && !o.date_to && !o.similar_to;
  let deb;
  f.addEventListener("input", () => { clearTimeout(deb); deb = setTimeout(async () => {
    const rs = rules();
    if (empty(rs)) { preview.replaceChildren(h("span", { class: "meta" }, "No rules: you'll add items by hand.")); return; }
    try { const p = await api("/api/spaces/preview", { method: "POST", json: rs });
      preview.replaceChildren(h("span", {}, h("span", { class: "count" }, `${p.count} item${p.count === 1 ? "" : "s"} match`), " right now"),
        p.examples.map((x) => h("span", { class: "meta clamp3" }, `• ${SRC[x.source_type]?.label}: ${x.text}`)));
    } catch (e) { preview.replaceChildren(h("span", { class: "meta" }, "⚠ " + e.message)); } }, 350); });
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim(), color, rules: rules() };
    try {
      const sp = existing ? await api(`/api/spaces/${existing.id}`, { method: "PATCH", json: body }) : await api("/api/spaces", { method: "POST", json: body });
      closeOverlays(); toast(existing ? "Space updated" : "Space created, filling it now…"); await loadSpaces(); location.hash = `#/space/${sp.id}`;
      setTimeout(() => { if (location.hash === `#/space/${sp.id}`) router(); }, 2500);
    } catch (err) { fail(err); }
  });
  modal(f, "wide");
  if (existing) f.dispatchEvent(new Event("input"));
}

// ---------- Sources ----------
async function viewSources(view) {
  const sources = await api("/api/sources");
  const counts = sources.reduce((a, s) => (a[s.type] = (a[s.type] || 0) + 1, a), {});
  const processing = sources.filter((s) => s.status === "pending" || s.status === "processing").length, failed = sources.filter((s) => s.status === "failed").length;
  let filter = "";
  const tbody = h("tbody");
  const fileIn = h("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg,.txt", class: "sr", id: "file-in", multiple: true });
  const drop = h("label", { class: "drop", for: "file-in" }, h("span", { class: "ic" }, icon("upload", 22)),
    h("span", { style: "flex:1" }, h("span", { style: "display:block;font-size:16px;font-weight:700" }, "Drop PDFs, images or a WhatsApp .txt export"),
      h("span", { class: "meta", style: "font-size:13.5px" }, "Up to 20 MB each. Text in photos and scanned PDFs is read automatically.")), h("span", { class: "btn primary" }, "Browse files"), fileIn);
  const upload = async (files) => { for (const file of files) { const fd = new FormData(); fd.append("file", file); toast(`Uploading ${file.name}…`);
    try { const r = await api("/api/upload", { method: "POST", body: fd }); toast(r.created ? `Saved ✓ ${file.name}, reading it now` : `Already saved: ${file.name}`); } catch (e) { fail(e); } } viewSources(view); };
  fileIn.addEventListener("change", () => upload([...fileIn.files]));
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
  const statusEl = (s) => s.status === "done" ? h("span", { class: "st" }, "READY") : s.status === "failed" ? h("span", { class: "st bad", title: s.last_error || "" }, "FAILED") : h("span", { class: "st wait" }, s.status === "processing" ? "READING…" : "QUEUED");
  const render = () => tbody.replaceChildren(...sources.filter((s) => !filter || s.type === filter).map((s) => h("tr", {},
    h("td", {}, h("span", { class: "tico", style: `background:${SRC[s.type]?.color}` }, icon(SRC[s.type]?.icon || "file"))),
    h("td", {}, h("span", { class: "ttl", title: s.title || "" }, s.title || SRC[s.type]?.label), h("span", { class: "meta" }, `${s.segments} item${s.segments === 1 ? "" : "s"}${s.events ? ` · ${s.events} event${s.events === 1 ? "" : "s"}` : ""}`)),
    h("td", { class: "muted" }, SRC[s.type]?.label), h("td", { class: "muted" }, fDay(s.created_at)), h("td", {}, statusEl(s)),
    h("td", {}, h("button", { class: "ibtn", type: "button", "aria-label": `Delete ${s.title || s.type}`, onclick: async () => {
      if (await confirmBox({ title: `Delete “${s.title || SRC[s.type]?.label}”?`, text: "This removes the source, its search index and its events.",
        items: [`${s.events} event${s.events === 1 ? "" : "s"} and their reminders`, `${s.segments} searchable item${s.segments === 1 ? "" : "s"}`, "The stored file, if any", "Its citations in future answers"], ok: "Delete source" })) {
        try { await api(`/api/sources/${s.id}`, { method: "DELETE" }); toast("Deleted"); viewSources(view); loadSpaces(); } catch (e) { fail(e); } } } }, icon("trash"))))));
  const chips = h("div", { class: "chips" }, [["", `All · ${sources.length}`], ["whatsapp", `WhatsApp · ${counts.whatsapp || 0}`], ["gmail", `Gmail · ${counts.gmail || 0}`],
    ["telegram", `Telegram · ${counts.telegram || 0}`], ["upload", `Uploads · ${counts.upload || 0}`]].map(([k, l]) => h("button", { type: "button", class: "chip" + (k === filter ? " on" : ""), onclick: (e) => {
      filter = k; chips.querySelectorAll(".chip").forEach((c) => c.classList.remove("on")); e.currentTarget.classList.add("on"); render(); } }, l)));
  render();
  view.replaceChildren(h("div", { class: "page" },
    hero({ badge: `${sources.length} sources${processing ? ` · ${processing} processing` : ""}${failed ? ` · ${failed} failed` : ""}`, badgeCls: "orange", title: "Sources", text: "Everything you've saved, where it came from and whether it's ready to search." }),
    drop, chips, sources.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {},
      h("th", { scope: "col", style: "width:64px" }, h("span", { class: "sr" }, "Type")), h("th", { scope: "col" }, "Title"), h("th", { scope: "col" }, "Origin"), h("th", { scope: "col" }, "Added"),
      h("th", { scope: "col" }, "Status"), h("th", { scope: "col", style: "width:70px" }, h("span", { class: "sr" }, "Actions")))), tbody))
      : h("div", { class: "soft-empty" }, "Nothing saved yet. Forward something to the Telegram bot or drop a file above.")));
  if (processing) setTimeout(() => { if (parseHash().route === "sources") viewSources(view); }, 4000);
}

// ---------- boot ----------
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#q").value.trim(); if (!q) return;
  location.hash = S.mode === "ai" ? `#/ask?q=${encodeURIComponent(q)}` : `#/search?mode=${S.mode}&q=${encodeURIComponent(q)}`;
});
$("#mode-btn").addEventListener("click", toggleModePop);
document.addEventListener("click", (e) => { if (!e.target.closest("#search-form")) $("#mode-pop").classList.add("hidden"); });
$("#new-space").addEventListener("click", () => spaceDialog());
$("#theme-btn").addEventListener("click", () => {
  const light = document.documentElement.classList.toggle("light");
  try { localStorage.setItem("cm-theme", light ? "light" : "dark"); } catch {}
});
$("#avatar").addEventListener("click", (e) => menu(e.currentTarget, [{ label: S.me?.name || "Account" },
  { text: "Log out", icon: "x", run: async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); } }]));
$("#avatar").classList.add("menu-trigger");
$("#avatar").parentElement.style.position = "sticky";

function liveRefresh() { if (parseHash().route === "today" && !document.querySelector(".overlay,.drawer")) viewToday($("#view"), {}).catch(() => {}); }
window.addEventListener("focus", liveRefresh);
document.addEventListener("visibilitychange", () => { if (!document.hidden) liveRefresh(); });

(async function boot() {
  try { setMode(localStorage.getItem("cm-mode") || "hybrid"); } catch { setMode("hybrid"); }
  try { S.me = await api("/api/me"); } catch { return; }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  renderSideBits();
  await loadSpaces();
  router();
  S.timer = setInterval(liveRefresh, 60000);
})();
