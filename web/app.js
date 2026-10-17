// Casa web UI - shell, helpers and router. No build step: plain ES modules.
//
// A page is a module in /static/pages/ exporting
//   default { id, title, icon, render(root, args) -> cleanup?() }
// `args` is the rest of the hash route (#/media/movie/123 -> ["movie", "123"]).
// Pages listen to live updates through `bus` (events: "entity", "entity_removed",
// "notification", "state", plus any type the hub publishes, e.g. "media").

export const bus = new EventTarget();
export const store = { entities: new Map(), plugins: [], homekit: {}, rooms: [], unread: 0, name: "Casa", attention: 0, attentionItems: [] };

// ---------------------------------------------------------------- helpers

export async function api(path, { method, body, raw } = {}) {
  const opts = { method: method || (body !== undefined ? "POST" : "GET"), headers: {} };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path.startsWith("/") ? path : `/api/${path}`, opts);
  if (r.status === 401) { showLogin(); throw new Error("login required"); }
  if (raw) return r;
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.error || `HTTP ${r.status}`);
  return data;
}

/** h("div.card.row", {onclick}, child, [children], "text") */
export function h(tag, props, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = name === "svg" ? document.createElementNS("http://www.w3.org/2000/svg", "svg")
                            : document.createElement(name || "div");
  if (classes.length) el.setAttribute("class", classes.join(" "));
  if (props && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) {
    children.unshift(props); props = null;
  }
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.setAttribute("class", [el.getAttribute("class"), v].filter(Boolean).join(" "));
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "html") el.innerHTML = v;
    else if (k in el && !(el instanceof SVGElement) && k !== "list" && typeof el[k] !== "function") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
  network: '<rect x="9" y="2" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="16" y="16" width="6" height="6" rx="1"/><path d="M5 16v-3h14v3M12 8v5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  thermo: '<path d="M14 14.8V4a2 2 0 1 0-4 0v10.8a4 4 0 1 0 4 0z"/>',
  drop: '<path d="M12 2.7 6.3 8.4a8 8 0 1 0 11.4 0z"/>',
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.4-.5-2-1-3-1.1-2.1-.2-4 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.2.4-2.3 1-3.3.3 1.4 1.3 2.8 2.5 2.8z"/>',
  shower: '<path d="M4 4l2.5 2.5M13.5 6.5 9 2 6.5 4.5 11 9M9 2l11 11M14 13l-1 1M10 16l-1 1M17 16l-1 1M13 19l-1 1M20 19l-1 1"/>',
  bulb: '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2M18 8v5a6 6 0 0 1-12 0V8z"/>',
  speaker: '<rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><path d="M12 6h.01"/>',
  tv: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="m17 2-5 5-5-5"/>',
  appletv: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  play: '<path d="M7 4v16l13-8z"/>',
  pause: '<path d="M7 4h3v16H7zM14 4h3v16h-3z"/>',
  next: '<path d="M5 4v16l10-8zM19 5v14"/>',
  prev: '<path d="M19 20V4L9 12zM5 19V5"/>',
  power: '<path d="M18.4 6.6a9 9 0 1 1-12.8 0M12 2v10"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  volume: '<path d="M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>',
  mute: '<path d="M11 5 6 9H2v6h4l5 4zM22 9l-6 6M16 9l6 6"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/>',
  up: '<path d="m18 15-6-6-6 6"/>', down: '<path d="m6 9 6 6 6-6"/>',
  left: '<path d="m15 18-6-6 6-6"/>', right: '<path d="m9 18 6-6-6-6"/>',
  back: '<path d="M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3"/>',
  remote: '<rect x="7" y="2" width="10" height="20" rx="3"/><circle cx="12" cy="8" r="2"/><path d="M12 14h.01M12 17h.01"/>',
  sensor: '<path d="M2 12h3l3-8 4 16 3-8h7"/>',
  wifi: '<path d="M5 12.6a10 10 0 0 1 14 0M8.5 16.1a5 5 0 0 1 7 0M2 8.8a15 15 0 0 1 20 0M12 20h.01"/>',
  nas: '<rect x="3" y="3" width="18" height="8" rx="2"/><rect x="3" y="13" width="18" height="8" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  star: '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  edit: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.8 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  chart: '<path d="M3 3v18h18M7 15l4-4 3 3 6-6"/>',
  utensils: '<path d="M4 2v7a3 3 0 0 0 6 0V2M7 2v20M17 22V2c-2 1.5-3 4-3 7v4h3"/>',
  cart: '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.4a1 1 0 0 0 1 .8h9.6a1 1 0 0 0 1-.8L21 7H6"/>',
  bluetooth: '<path d="m7 7 10 10-5 5V2l5 5L7 17"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  storage: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  flag: '<path d="M4 22V4s1.5-1 5-1 5 2 8 2 3-1 3-1v11s-1 1-3 1-5-2-8-2-5 1-5 1"/>',
  homekit: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/><rect x="9" y="11" width="6" height="6" rx="1"/>',
};

export function icon(name, cls = "") {
  const s = h("svg", { viewBox: "0 0 24 24", class: `i ${cls}`.trim() });
  s.innerHTML = ICONS[name] || ICONS.sensor;
  return s;
}

export function toast(msg, bad = false) {
  const el = h(`div.toast${bad ? ".bad" : ""}`, msg);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), bad ? 5000 : 2600);
}

/** Modal sheet; returns {el, close}. */
export function sheet(title, content, { wide = false, onClose } = {}) {
  const close = () => { bd.remove(); document.removeEventListener("keydown", esc); onClose?.(); };
  const esc = (e) => e.key === "Escape" && close();
  const body = h("div.stack", content);
  const bd = h("div.backdrop", { onclick: (e) => e.target === bd && close() },
    h(`div.sheet${wide ? ".wide" : ""}`,
      h("header", h("h2.ellipsis", title), h("div.spacer"),
        h("button.icon.ghost", { onclick: close, "aria-label": "Close" }, icon("x"))),
      body));
  document.addEventListener("keydown", esc);
  document.body.append(bd);
  return { el: body, close };
}

export function ensureCss(href) {
  if (!document.querySelector(`link[href="${href}"]`))
    document.head.append(h("link", { rel: "stylesheet", href }));
}

/** Run an async action with button feedback + error toast. */
export async function act(fn, btn) {
  btn && (btn.disabled = true);
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn && (btn.disabled = false); }
}

export const callEntity = (id, action, params = {}) =>
  api(`entities/${encodeURIComponent(id)}/${action}`, { body: params });

export const debounce = (fn, ms = 300) => {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

export const fmt = {
  bytes(n) {
    if (!n && n !== 0) return "";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
  },
  ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  },
  duration(sec) {
    if (!sec && sec !== 0) return "";
    sec = Math.round(sec);
    const h_ = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return h_ ? `${h_}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  },
  temp: (v) => (v === null || v === undefined ? "–" : (+v).toFixed(1)),
};

// ---------------------------------------------------------------- state + events

export async function refreshState() {
  const s = await api("state");
  store.entities = new Map(s.entities.map((e) => [e.id, e]));
  Object.assign(store, { plugins: s.plugins, homekit: s.homekit, rooms: s.rooms, unread: s.unread,
                         name: s.name, notify_channels: s.notify_channels, onboarding: s.onboarding });
  document.title = s.name;
  store.attention = s.attention || 0;
  updateBadge();
  refreshAttention().catch(() => {});
  bus.dispatchEvent(new CustomEvent("state", { detail: s }));
  return s;
}

/** Re-read the attention list (items for the Home banner, counts for the nav badge). */
export async function refreshAttention() {
  const items = await api("attention");
  store.attentionItems = items;
  store.attention = items.filter((i) => i.level !== "info").length;
  updateBadge();
  bus.dispatchEvent(new CustomEvent("attentionchange", { detail: items }));
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "entity") store.entities.set(msg.data.id, msg.data);
    if (msg.type === "entity_removed") store.entities.delete(msg.data.id);
    if (msg.type === "notification") { store.unread++; updateBadge(); toast(`🔔 ${msg.data.title}`); }
    if (msg.type === "plugins" || msg.type === "attention") refreshAttention().catch(() => {});
    bus.dispatchEvent(new CustomEvent(msg.type, { detail: msg.data }));
  };
  // EventSource reconnects by itself; resync once it's back
  let wasDown = false;
  es.onerror = () => { wasDown = true; };
  es.onopen = () => { if (wasDown) { wasDown = false; refreshState().then(render); } };
}

function updateBadge() {
  document.querySelectorAll("[data-unread]").forEach((el) => el.classList.toggle("hidden", !store.unread));
  const hasErr = (store.attentionItems || []).some((i) => i.level === "error");
  document.querySelectorAll("[data-attn]").forEach((el) => {
    el.classList.toggle("hidden", !store.attention);
    el.classList.toggle("err", hasErr);
    el.textContent = store.attention;
  });
}

// ---------------------------------------------------------------- notifications

export async function openNotifications() {
  const list = await api("notifications");
  const s = sheet("Notifications", list.length ? h("div.list", list.map((n) =>
    h("div.item", { style: { alignItems: "flex-start", cursor: n.url ? "pointer" : "" },
                    onclick: () => n.url && (location.href = n.url) },
      n.image ? h("img", { src: n.image, style: { width: "44px", height: "66px", objectFit: "cover", borderRadius: "6px" } })
              : h("div.tile", h("div.ico", icon("bell"))),
      h("div.stack", { style: { gap: "2px", minWidth: 0 } },
        h("div.row", h("b", n.title), !n.read && h("span.chip.accent", "new")),
        n.body && h("div.small", n.body),
        h("div.tiny.muted", fmt.ago(n.ts)))))) : h("div.empty", "Nothing yet."));
  if (store.unread) { await api("notifications/read", { body: {} }); store.unread = 0; updateBadge(); }
  return s;
}

// ---------------------------------------------------------------- login

function showLogin() {
  if (document.querySelector("#login")) return;
  const pw = h("input", { type: "password", placeholder: "Password", autocomplete: "current-password" });
  const go = async (e) => {
    e.preventDefault();
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ password: pw.value }) });
    if (r.ok) location.reload(); else toast("Wrong password", true);
  };
  document.body.append(h("div.backdrop#login",
    h("form.sheet.stack", { onsubmit: go, style: { maxWidth: "360px" } },
      h("h2", "Casa"), pw, h("button.primary.lg", { type: "submit" }, "Unlock"))));
  pw.focus();
}

// ---------------------------------------------------------------- router

const PAGES = [
  { id: "home", title: "Home", icon: "home" },
  { id: "media", title: "Media", icon: "film" },
  { id: "recipes", title: "Recipes", icon: "utensils" },
  { id: "history", title: "History", icon: "chart" },
  { id: "network", title: "Network", icon: "network" },
  { id: "settings", title: "Settings", icon: "settings" },
  { id: "welcome", title: "Welcome", icon: "home", hidden: true },  // first-run setup
];
const loaded = {};
let cleanup = null;

async function render() {
  const [id = "home", ...args] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  document.querySelectorAll("nav a.tab").forEach((a) => a.classList.toggle("active", a.dataset.id === page.id));
  const main = document.querySelector("main");
  if (typeof cleanup === "function") { try { cleanup(); } catch {} }
  cleanup = null;
  try {
    loaded[page.id] ??= (await import(`./pages/${page.id}.js?v=${window.CASA_V}`)).default;
    main.replaceChildren();
    cleanup = await loaded[page.id].render(main, args);
  } catch (e) {
    console.error(e);
    main.replaceChildren(h("div.empty", `Couldn't load ${page.title}: ${e.message}`));
  }
}

/** Same page, different args, without a full re-render when the page supports it. */
export const go = (hash) => { location.hash = hash; };

function shell() {
  const nav = h("nav.side",
    h("div.brand", "Casa"),
    PAGES.filter((p) => !p.hidden).map((p) => h("a.tab", { href: `#/${p.id}`, "data-id": p.id }, icon(p.icon), h("span", p.title),
      p.id === "settings" && h("span.count-badge.hidden", { "data-attn": "", title: "Needs attention" }))),
    h("div.spacer"),
    h("a.tab", { href: "javascript:void 0", onclick: openNotifications },
      icon("bell"), h("span", "Alerts"), h("span.dot.hidden", { "data-unread": "" })));
  document.getElementById("app").replaceChildren(nav, h("main"));
}

async function boot() {
  ensureCss(`/static/pages/attention.css?v=${window.CASA_V}`);
  shell();
  try { await refreshState(); } catch (e) { if (e.message !== "login required") toast(e.message, true); return; }
  if (!location.search.includes("static")) connectEvents();  // ?static: no live updates (screenshots)
  window.addEventListener("hashchange", render);
  // first run: guided setup (skippable; Settings can start it again)
  if (store.onboarding && !location.hash) location.hash = "#/welcome";
  render();
}

boot();
