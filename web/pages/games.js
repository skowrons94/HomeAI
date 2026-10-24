// Games: what is on the NAS, what is on the Steam Deck, one tap to install, and a way to find more
// (hub/plugins/games/). Route: #/games.
// Two tabs: Library (browse, detail sheet, install) and Find (torrent search that downloads into the
// games folder, plus the downloads in progress). The gear opens the settings sheet.
// Polling: 2 s while something installs, 3 s while a download is active on the Find tab, else 30 s.
import { api, h, icon, toast, ensureCss, act, fmt, t, sheet } from "../app.js";

const P = (path, opts) => api(`p/games/${path}`, opts);
const KEY = "casa.games.view";
const KIND = () => ({ portable: t("PC"), installer: t("PC installer"), native: t("Linux"), rom: t("Emulation") });
const SORTS = () => [["az", t("A–Z")], ["new", t("Recently added")], ["size", t("Size")], ["system", t("System")]];

let data = { games: [], problems: [], deck: {}, systems: [] };
let loaded = false;
// filter: "all" | "installed" | "update" | "p:<platform>"
let view = { tab: "library", filter: "all", sort: "az", fplat: "pc" };
let query = "";
let root = null;
let timer = null;
let openId = null;      // game whose detail sheet is open
let detail = null;      // {el, close, sig}
const els = {};
const fx = { platforms: [], q: "", results: [], providers: [], page: 1, more: false, error: "", off: false,
  loading: false, searched: false, downloads: [], seen: new Map() };

try { view = { ...view, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* private mode */ }
const saveView = () => { try { localStorage.setItem(KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const busyNow = () => data.games.some((g) => g.state === "installing");
const activeDl = () => fx.downloads.filter((d) => d.state === "downloading" || d.state === "extracting");
const platName = (k) => ({ pc: t("PC"), linux: t("Linux") }[k] || String(k || "").toUpperCase());
const gtitle = (g) => g.meta?.title || g.title;
const year = (g) => (g.meta?.released || "").slice(0, 4);

// ---- data ---------------------------------------------------------------------------------

async function loadLibrary(path = "library", opts) {
  try { data = await P(path, opts); loaded = true; }
  catch (e) { if (root?.isConnected) toast(e.message, true); loaded = true; }
  drawLibrary();
}

async function loadDownloads() {
  try {
    const list = await P("find/downloads");
    fx.downloads = Array.isArray(list) ? list : (list?.downloads || []);
  } catch { fx.downloads = []; }
  // a download that just finished adds a game to the library: rescan comes from the hub, we reload
  let finished = false;
  for (const d of fx.downloads) {
    if (fx.seen.get(d.hash) && fx.seen.get(d.hash) !== "done" && d.state === "done") finished = true;
    fx.seen.set(d.hash, d.state);
  }
  drawDownloads();
  drawTabs();
  return finished;
}

function schedule() {
  clearTimeout(timer);
  if (!root?.isConnected) return;
  const ms = busyNow() ? 2000 : (view.tab === "find" && activeDl().length) ? 3000 : 30000;
  timer = setTimeout(tick, ms);
}

async function tick() {
  if (!root?.isConnected) return;
  const [, finished] = await Promise.all([loadLibrary(), loadDownloads()]);
  if (finished) await loadLibrary();
  schedule();
}

const refresh = async (path, opts) => { await loadLibrary(path, opts); schedule(); };

// ---- small pieces -------------------------------------------------------------------------

const svg = (vb, inner, cls) => { const s = h("svg", { viewBox: vb, class: cls }); s.innerHTML = inner; return s; };
const sortIcon = () => svg("0 0 24 24", '<path d="M4 7h10M4 12h7M4 17h4M17 5v14M17 19l-3-3M17 19l3-3"/>', "i");

const hue = (s) => { let n = 0; for (const c of String(s)) n = (n * 31 + c.charCodeAt(0)) % 360; return n; };
const initials = (title) => {
  const w = title.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2))).toUpperCase();
};

function cover(g, cls = "") {
  if (g.cover) return h(`img.gm-cover${cls}`, { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, loading: "lazy", alt: "" });
  return h(`div.gm-cover.none${cls}`, { style: `--gh:${hue(g.platform || g.system || g.kind)}` },
    h("span.gm-init", initials(gtitle(g))), h("span.gm-sys", platName(g.platform || "pc")));
}

function ring(p) {
  const c = 2 * Math.PI * 15;
  return svg("0 0 36 36", `<circle cx="18" cy="18" r="15" class="bg"/><circle cx="18" cy="18" r="15" class="fg" stroke-dasharray="${(c * Math.min(1, p || 0)).toFixed(1)} ${c.toFixed(1)}"/>`, "gm-ring");
}

const pct = (g) => Math.round((g.progress || 0) * 100);

/** Corner badge over a cover. */
function cornerBadge(g) {
  if (g.state === "installing") return h("span.gm-corner.busy", ring(g.progress), h("b", `${pct(g)}`));
  if (g.state === "installed") return h("span.gm-corner.ok", icon("check"));
  if (g.state === "update") return h("span.gm-corner.upd", icon("download"), t("Update"));
  if (g.state === "failed") return h("span.gm-corner.bad", icon("x"));
  return null;
}

function stateChip(g) {
  const m = {
    installed: [t("Installed"), "ok"], update: [t("Update available"), "warn"], installing: [t("Installing…"), "accent"],
    failed: [t("Failed"), "bad"], needs_manual: [t("Needs a manual step"), "accent"],
  }[g.state];
  return m ? h(`span.chip.${m[1]}`, m[0]) : null;
}

// ---- actions ------------------------------------------------------------------------------

const install = (g, e) => act(async () => {
  await P("install", { body: { id: g.id } }); toast(t("Sent to the Steam Deck")); await refresh();
}, e.currentTarget);

const remove = (g, e) => confirm(t("Remove {title} from the Steam Deck?", { title: gtitle(g) })) && act(async () => {
  const r = await P("uninstall", { body: { id: g.id } });
  if (r.note) toast(r.note);
  await refresh();
}, e.currentTarget);

const applySteam = (e) => confirm(t("Close and reopen Steam on the Deck now?")) && act(async () => {
  await P("apply", { body: { restart: !!data.deck?.steam_running } });
  toast(t("Added to Steam")); await refresh();
}, e.currentTarget);

function actions(g) {
  const online = data.deck?.online;
  const label = {
    not_installed: t("Install"), installed: t("Reinstall"), update: t("Update to {v}", { v: g.version }),
    failed: t("Try again"), needs_manual: t("Download again"),
  }[g.state] || t("Installing…");
  const primary = g.state === "not_installed" || g.state === "update";
  const rm = (g.state === "installed" || g.state === "update")
    ? h("button.danger", { disabled: !online, onclick: (e) => remove(g, e) }, icon("trash"), t("Remove")) : null;
  const steam = online && data.deck.pending && g.state === "installed" && g.kind !== "installer"
    ? h("button", { onclick: applySteam }, t("Apply to Steam")) : null;
  return h("div.gm-actions",
    h("button" + (primary ? ".primary" : ""), {
      disabled: !online || g.state === "installing", title: online ? "" : t("The Steam Deck is not reachable"),
      onclick: (e) => install(g, e) }, icon("download"), label),
    steam, rm);
}

// ---- library: cards -----------------------------------------------------------------------

function card(g) {
  const meta = [platName(g.platform), year(g)].filter(Boolean).join(" · ") || fmt.bytes(g.size);
  return h("button.gm-tile", { onclick: () => openDetail(g.id), "aria-label": gtitle(g) },
    h("div.gm-art", cover(g), cornerBadge(g),
      g.state === "installing" ? h("div.progress", h("div", { style: { width: `${pct(g)}%` } })) : null),
    h("div.gm-title", gtitle(g)),
    h("div.gm-meta", meta));
}

// ---- detail sheet -------------------------------------------------------------------------

const row = (k, v) => (v || v === 0 ? h("div.gm-kv", h("span", k), h("b", v)) : null);

function viewShot(url) {
  const close = () => { ov.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => e.key === "Escape" && close();
  const ov = h("div.gm-shot", { onclick: close }, h("img", { src: url, alt: "" }),
    h("button.icon", { "aria-label": t("Close"), onclick: close }, icon("x")));
  document.addEventListener("keydown", esc);
  document.body.append(ov);
}

function summary(text) {
  const p = h("p.gm-sum.clamp", text);
  if (text.length < 220) { p.classList.remove("clamp"); return p; }
  const btn = h("button.ghost.gm-more", { onclick: () => {
    const c = p.classList.toggle("clamp"); btn.textContent = c ? t("More") : t("Less");
  } }, t("More"));
  return h("div", p, btn);
}

function hero(g) {
  const m = g.meta;
  const bg = m?.has_hero ? h("img.gm-hero-img", { src: `/api/p/games/hero/${encodeURIComponent(g.id)}`, alt: "" })
    : g.cover ? h("img.gm-hero-img.blur", { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, alt: "" })
      : h("div.gm-hero-img.none", { style: `--gh:${hue(g.platform || g.system || g.kind)}` });
  return h("div.gm-hero", bg);
}

function detailBody(g) {
  const m = g.meta;
  const sub = [year(g), m?.developer].filter(Boolean).join(" · ");
  const sub2 = [...new Set([KIND()[g.kind], g.system && g.system.toUpperCase()].filter(Boolean))].join(" · ");
  const shots = (m?.screenshots || []).filter(Boolean);
  return [
    hero(g),
    h("div.gm-dhead", cover(g, ".big"),
      h("div.gm-dinfo", h("div.gm-dtitle", gtitle(g)),
        sub ? h("div.gm-sub", sub) : null, h("div.gm-meta", sub2),
        h("div.gm-chips-in", stateChip(g),
          m?.rating != null ? h("span.chip.gm-rate", icon("star"), `${fmt.num(m.rating / 10, 1)}/10`) : null))),
    m?.genres?.length ? h("div.gm-genres", m.genres.map((x) => h("span.chip", x))) : null,
    g.state === "installing" ? h("div.stack", { style: { gap: "6px" } },
      h("div.progress", h("div", { style: { width: `${pct(g)}%` } })),
      h("div.gm-note", `${pct(g)}%${g.message ? " · " + g.message : ""}`)) : null,
    g.message && g.state !== "installing" ? h("div.gm-banner" + (g.state === "failed" ? ".bad" : ""), g.message) : null,
    g.state === "needs_manual" ? h("div.gm-banner", t("This is an installer: it was downloaded to the Deck but you must run it there (Desktop Mode, with Proton).")) : null,
    g.note && g.note !== g.message ? h("div.gm-note", g.note) : null,
    m?.summary ? summary(m.summary) : null,
    shots.length ? h("div.gm-shots", shots.map((u) => h("button.gm-shotbtn", { onclick: () => viewShot(u), "aria-label": t("Screenshot") },
      h("img", { src: u, loading: "lazy", alt: "" })))) : null,
    actions(g),
    h("div.gm-kvs",
      row(t("Version"), g.version ? `v${g.version}` : ""),
      row(t("Installed on the Deck"), g.installed_version ? `v${g.installed_version}` : (g.state === "installed" || g.state === "update" ? t("Yes") : "")),
      row(t("Size"), fmt.bytes(g.size)),
      row(t("Files"), g.files != null ? fmt.num(g.files) : ""),
      row(t("Added"), g.added ? fmt.date(g.added, { weekday: false }) : ""),
      row(t("Folder on the NAS"), g.path)),
    h("button.ghost.gm-fix", { onclick: () => fixMatch(g) }, icon("edit"), t("Wrong game? Fix match")),
  ].filter(Boolean);
}

const sig = (g) => JSON.stringify([g.state, g.progress != null ? pct(g) : null, g.message, g.installed_version, g.meta?.igdb_id,
  g.cover, data.deck?.online, data.deck?.pending]);

function openDetail(id) {
  const g = data.games.find((x) => x.id === id);
  if (!g) return;
  openId = id;
  detail?.close();
  detail = sheet("", detailBody(g), { onClose: () => { openId = null; detail = null; } });
  detail.el.parentElement.classList.add("gm-dsheet");
  detail.sig = sig(g);
}

function redrawDetail() {
  if (!openId || !detail) return;
  const g = data.games.find((x) => x.id === openId);
  if (!g) { detail.close(); return; }
  if (sig(g) === detail.sig) return;
  detail.sig = sig(g);
  detail.el.replaceChildren(...detailBody(g));
}

function fixMatch(g) {
  const list = h("div.gm-list");
  const msg = h("div.gm-note");
  const input = h("input", { type: "search", value: gtitle(g), autocomplete: "off", enterkeyhint: "search" });
  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    list.replaceChildren(h("div.empty", h("span.spin")));
    msg.textContent = "";
    try {
      const d = await P(`meta/search?q=${encodeURIComponent(q)}&platform=${encodeURIComponent(g.platform || "")}`);
      const rs = d.results || [];
      msg.textContent = rs.length ? "" : t("No results.");
      list.replaceChildren(...rs.map((r) => h("button.gm-pick", { onclick: (e) => act(async () => {
        await P("meta/match", { body: { id: g.id, igdb_id: r.igdb_id } });
        toast(t("Match updated")); s.close(); await refresh();
      }, e.currentTarget) },
      r.cover_url ? h("img", { src: r.cover_url, loading: "lazy", alt: "" }) : h("div.gm-pick-ph", initials(r.title)),
      h("span.gm-pick-t", h("b", r.title), h("span.gm-meta", [r.year, (r.platforms || []).slice(0, 3).join(", ")].filter(Boolean).join(" · "))))));
    } catch (e) { list.replaceChildren(); msg.textContent = e.message; }
  };
  const s = sheet(t("Fix match"), [
    h("div.gm-note", t("Search the game as it is called in the database, then pick the right one.")),
    h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); search(); } }, input,
      h("button.primary", { type: "submit" }, icon("search"), t("Search"))),
    msg, list]);
  search();
}

// ---- find tab -----------------------------------------------------------------------------

function platRow() {
  els.fplat.replaceChildren(...fx.platforms.map((p) => chip(p.label, null, view.fplat === p.key, () => {
    view.fplat = p.key; saveView(); drawPlat();
    if (fx.q.trim().length >= 2) search(true);
  })));
}
const drawPlat = platRow;

async function loadPlatforms() {
  try { fx.platforms = await P("find/platforms"); } catch { fx.platforms = []; }
  if (!Array.isArray(fx.platforms) || !fx.platforms.length) fx.platforms = [{ key: "pc", label: t("PC") }];
  if (!fx.platforms.some((p) => p.key === view.fplat)) view.fplat = fx.platforms[0].key;
  drawPlat();
}

let findSeq = 0;   // ignore answers of superseded searches (trackers can take 10 s)
async function search(fresh = true) {
  const q = fx.q.trim();
  if (q.length < 2) { fx.error = t("Type at least 2 characters"); drawResults(); return; }
  const my = ++findSeq, plat = view.fplat;
  if (fresh) { fx.page = 1; fx.results = []; } else fx.page += 1;
  fx.loading = true; fx.error = ""; fx.off = false; fx.searched = true;
  drawResults();
  try {
    const d = await P(`find?q=${encodeURIComponent(q)}&platform=${encodeURIComponent(plat)}&page=${fx.page}`);
    if (my !== findSeq) return;
    fx.off = !!d.disabled;
    fx.plat = plat;   // the platform these results were searched for: downloads go there
    fx.results = fresh ? (d.results || []) : fx.results.concat(d.results || []);
    fx.providers = d.providers || [];
    fx.more = !!d.has_more;
    fx.error = d.error || "";
  } catch (e) {
    if (my !== findSeq) return;
    fx.error = e.message;
  }
  fx.loading = false;
  drawResults();
}

function confirmDownload(r) {
  const plat = fx.plat || view.fplat;
  const dest = plat === "pc" ? "games/pc" : `games/emu/${plat}`;
  const s = sheet(t("Download"), [
    h("div.gm-dtitle", { style: { fontSize: "17px" } }, r.title),
    h("div.gm-meta", [r.size ? fmt.bytes(r.size) : "", r.seeders != null ? t("{n} seeders", { n: r.seeders }) : ""].filter(Boolean).join(" · ")),
    h("div.gm-banner.info", icon("nas"), h("span", t("Downloads to {dest} on the NAS", { dest }))),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      await P("find/download", { body: { title: r.title, platform: plat, magnet: r.magnet, url: r.url } });
      toast(t("Download started")); s.close(); await loadDownloads(); schedule();
    }, e.currentTarget) }, icon("download"), t("Download")),
  ]);
}

function resultRow(r) {
  const srcs = r.sources?.length ? r.sources : (r.source ? [r.source] : []);
  return h("div.gm-res",
    h("div.gm-res-main",
      h("div.gm-res-title", r.title),
      h("div.gm-res-meta",
        r.size ? h("span", fmt.bytes(r.size)) : null,
        h("span.gm-seed", icon("up"), fmt.num(r.seeders || 0)),
        h("span.gm-leech", icon("down"), fmt.num(r.leechers || 0)),
        srcs.slice(0, 3).map((s) => h("span.chip", s)))),
    h("button.primary.gm-dl", { onclick: () => confirmDownload(r), "aria-label": t("Download") }, icon("download"), t("Download")));
}

function drawResults() {
  const out = [];
  const bad = fx.providers.filter((p) => p.ok === false && p.error);
  if (fx.off) {
    out.push(h("div.gm-card.gm-center", h("div.gm-ico", icon("search")),
      h("b", t("Search is off")),
      h("p.muted", t("Game search uses the Torrents page. Turn on the Torrents plugin and a tracker first.")),
      h("button.primary", { onclick: () => { location.hash = "#/torrents"; } }, t("Open Torrents"))));
  } else {
    if (bad.length) out.push(h("div.gm-warns", bad.map((p) => h("div.gm-warn", icon("wifi"), h("span", `${p.name}: ${p.error}`)))));
    if (fx.error && !fx.results.length) out.push(h("div.gm-warn.err", h("span", fx.error)));
    else if (fx.error) out.push(h("div.gm-warn", h("span", fx.error)));
    if (fx.results.length) out.push(h("div.gm-resl", fx.results.map(resultRow)));
    else if (fx.searched && !fx.loading && !fx.error) out.push(h("div.empty", t("No results.")));
    if (fx.loading) out.push(h("div.empty", h("span.spin")));
    else if (fx.more) out.push(h("div.gm-moreRow", h("button", { onclick: () => search(false) }, t("Load more"))));
    if (!fx.searched && !fx.loading) out.push(h("div.gm-hintcard", icon("search"),
      h("span", t("Search a game by name. Files are saved in your games folder and appear in the library once ready."))));
  }
  els.results.replaceChildren(...out);
}

function dlState(d) {
  if (d.state === "extracting") return t("Extracting…");
  if (d.state === "done") return t("Done");
  if (d.state === "needs_extraction") return d.error || t("Needs extracting on the NAS");
  if (d.state === "error") return d.error || t("Failed");
  const bits = [d.rate ? `${fmt.bytes(d.rate)}/s` : "", d.eta ? fmt.duration(d.eta) : ""].filter(Boolean);
  return bits.join(" · ") || t("Starting…");
}

function dlRow(d) {
  const active = d.state === "downloading" || d.state === "extracting";
  const p = Math.round((d.progress || 0) * 100);
  const bad = d.state === "error";
  return h("div.gm-dl-row",
    h("div.gm-dl-main",
      h("div.gm-res-title", d.title),
      active || d.state === "done" ? h("div.progress" + (d.state === "extracting" ? ".ind" : ""), h("div", { style: { width: d.state === "extracting" ? "100%" : `${p}%` } })) : null,
      h("div.gm-note" + (bad ? ".err" : ""), active && d.state === "downloading" ? `${p}% · ${dlState(d)}` : dlState(d))),
    h("button.icon.ghost", { "aria-label": active ? t("Cancel") : t("Remove"), onclick: (e) => act(async () => {
      await P(`find/downloads/${encodeURIComponent(d.hash)}`, { method: "DELETE" });
      await loadDownloads(); schedule();
    }, e.currentTarget) }, icon(active ? "x" : "trash")));
}

function drawDownloads() {
  if (!els.dl) return;
  els.dl.replaceChildren(...(fx.downloads.length ? [
    h("h3.gm-section", t("In progress"), h("span.gm-count", fmt.num(fx.downloads.length))),
    h("div.gm-card.gm-dls", fx.downloads.map(dlRow))] : []));
}

// ---- settings sheet -----------------------------------------------------------------------

async function openSettings() {
  ensureCss(`/static/pages/settings.css?v=${window.CASA_V}`);
  const { renderForm } = await import(`./settings.js?v=${window.CASA_V}`);
  const all = await api("settings");
  const p = all.plugins.find((x) => x.name === "games");
  if (!p) return;
  const schema = p.schema || [], values = p.values || {};
  const meta = (f) => /^(igdb|steamgriddb)\b/.test(f.key);
  const deck = (f) => f.key.startsWith("deck");
  const forms = [
    renderForm(schema.filter((f) => !deck(f) && !meta(f)), values),
    renderForm(schema.filter(deck), values),
    renderForm(schema.filter(meta), values),
  ];
  const status = h("div.stack", { style: { gap: "8px" } });
  const paint = (st) => {
    const d = st || data.deck || {};
    status.replaceChildren(...[
      h("div.row.wrap",
        h("span.chip" + (d.online ? ".ok" : ".bad"), d.online ? t("Online") : t("Offline")),
        d.online ? h("span.chip" + (d.steam_running ? ".accent" : ""), d.steam_running ? t("Steam running") : t("Steam closed")) : null,
        d.online ? h("span.chip" + (d.pending ? ".warn" : ""), t("{n} waiting for Steam", { n: d.pending || 0 })) : null),
      !d.online && (d.error || !d.configured) ? h("div.gm-note", d.error || t("Set the Steam Deck address and token in Settings → Games.")) : null].filter(Boolean));
  };
  paint();
  const md = data.metadata || {};
  const skipped = (data.problems || []).length ? h("details.gm-problems",
    h("summary", t("{n} items skipped", { n: data.problems.length })),
    ...data.problems.map((x) => h("div.gm-note", h("b", x.path), ": ", x.reason))) : null;
  const btn = (ico, label, fn, extra = {}) => h("button.gm-wide", { ...extra, onclick: (e) => act(() => fn(e), e.currentTarget) }, icon(ico), label);
  const sec = (title, ...kids) => h("section.gm-sec", h("h3", title), ...kids);

  const s = sheet(t("Games settings"), [
    sec(t("Library"), forms[0].el,
      h("div.gm-btns",
        btn("refresh", t("Rescan library"), async () => { await refresh("rescan", { method: "POST", body: {} }); toast(t("Library rescanned")); }))),
    sec(t("Steam Deck"), status, forms[1].el,
      h("div.gm-btns",
        btn("wifi", t("Test connection"), async () => {
          const r = await P("test", { body: {} });
          paint({ ...data.deck, online: r.ok, error: r.error, pending: r.pending, steam_running: r.steam_running });
          toast(r.ok ? t("Connected to the Steam Deck") : (r.error || t("The Steam Deck is not reachable")), !r.ok);
        }),
        btn("play", t("Apply to Steam"), async (e) => applySteam({ currentTarget: e.currentTarget }), { disabled: !data.deck?.online || !data.deck?.pending }))),
    sec(t("Metadata"),
      md.configured === false ? h("div.gm-note", t("Covers and descriptions come from IGDB. Add the free keys below to get them.")) : null,
      forms[2].el,
      h("div.gm-btns",
        btn("refresh", t("Refresh metadata"), async () => {
          await P("meta/refresh", { body: {} }); toast(t("Metadata refresh started")); await refresh();
        }))),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      const values = Object.assign({}, ...forms.map((f) => f.values()));
      for (const f of schema) if (f.type === "password" && !values[f.key]) delete values[f.key];   // empty = keep
      const r = await api("settings/plugins/games", { method: "PUT", body: { values } });
      if (r.error) { toast(r.error, true); return; }
      toast(t("Saved")); s.close(); await refresh();
    }, e.currentTarget) }, t("Save")),
    skipped,
  ].filter(Boolean), { wide: true });
  s.el.parentElement.classList.add("gm-ssheet");
}

// ---- library page -------------------------------------------------------------------------

function banner() {
  const d = data.deck || {};
  const out = [];
  if (loaded && !d.configured && !d.online)
    out.push(h("button.gm-slim", { onclick: openSettings }, icon("settings"), h("span", t("Set up your Steam Deck")), icon("right")));
  else if (loaded && !d.online)
    out.push(h("div.gm-slim", { title: d.error || "" }, icon("wifi"), h("span", t("Steam Deck offline. You can still browse the library."))));
  if (d.online && d.pending) {
    out.push(h("div.gm-banner",
      h("span", t("{n} waiting to be added to Steam. This closes and reopens Steam; use Desktop Mode.", { n: d.pending })),
      h("button.primary", { onclick: applySteam }, t("Apply to Steam"))));
  }
  return out;
}

function matches(g) {
  const q = query.trim().toLowerCase();
  if (q && !(g.title.toLowerCase().includes(q) || gtitle(g).toLowerCase().includes(q))) return false;
  const f = view.filter;
  if (f === "installed") return g.state === "installed" || g.state === "update";
  if (f === "update") return g.state === "update";
  if (f.startsWith("p:")) return g.platform === f.slice(2);
  return true;
}

const cmp = {
  az: (a, b) => gtitle(a).localeCompare(gtitle(b), undefined, { sensitivity: "base", numeric: true }),
  new: (a, b) => (b.added || 0) - (a.added || 0) || cmp.az(a, b),
  size: (a, b) => (b.size || 0) - (a.size || 0) || cmp.az(a, b),
  system: (a, b) => (a.platform || "").localeCompare(b.platform || "") || cmp.az(a, b),
};

function chip(label, count, on, fn) {
  return h("button.chip.gm-fchip" + (on ? ".on" : ""), { onclick: fn, "aria-pressed": on },
    label, count != null ? h("span.gm-count", fmt.num(count)) : null);
}

function drawChips() {
  const gs = data.games, sys = data.systems || [];
  const upd = gs.filter((g) => g.state === "update").length;
  const inst = gs.filter((g) => g.state === "installed" || g.state === "update").length;
  const set = (f) => () => { view.filter = f; saveView(); drawLibrary(); };
  // a saved platform filter that no longer exists would show an empty page
  if (view.filter.startsWith("p:") && !sys.some((s) => `p:${s.key}` === view.filter)) view.filter = "all";
  if (view.filter === "update" && !upd) view.filter = "all";
  els.chips.replaceChildren(
    chip(t("All"), gs.length, view.filter === "all", set("all")),
    chip(t("Installed"), inst, view.filter === "installed", set("installed")),
    upd ? chip(t("Updates"), upd, view.filter === "update", set("update")) : null,
    ...(sys.length > 1 ? sys.map((s) => chip(platName(s.key), s.count, view.filter === `p:${s.key}`, set(`p:${s.key}`))) : []));
  els.chips.parentElement.hidden = !gs.length;
}

function openSort() {
  const s = sheet(t("Sort"), SORTS().map(([k, l]) =>
    h("button.gm-opt" + (view.sort === k ? ".on" : ""), { onclick: () => { view.sort = k; saveView(); s.close(); drawLibrary(); } },
      h("span", l), view.sort === k ? icon("check") : null)));
}

function emptyState() {
  if (!loaded) return h("div.empty", h("span.spin"));
  if (!data.games.length) {
    const hint = h("div.gm-how", { hidden: true },
      h("p.muted", t("In the games folder, one folder is one game. Add cover.jpg to show a cover.")),
      h("div.gm-how-row", h("code", "pc/"), h("span", t("PC games: one folder each, with the .exe, .AppImage or installer"))),
      h("div.gm-how-row", h("code", "emu/snes/"), h("span", t("ROMs, one folder per system (snes, psx, …)"))),
      data.root ? h("div.gm-how-path", icon("folder"), h("span", data.root)) : null);
    const how = h("button.ghost", { onclick: () => { hint.hidden = !hint.hidden; how.firstChild.nodeValue = hint.hidden ? t("How to add games") : t("Hide"); } },
      t("How to add games"));
    return h("div.gm-card.gm-center.gm-emptycard", h("div.gm-ico", icon("play")),
      h("b", data.exists === false ? t("No games folder yet") : t("No games yet")),
      h("p.muted", t("Find a game and it lands here, ready to send to the Steam Deck.")),
      h("button.primary.lg", { onclick: () => setTab("find") }, icon("search"), t("Find games")),
      how, hint);
  }
  return h("div.empty", t("No matches."), " ",
    h("button.ghost", { onclick: () => { query = ""; els.q.value = ""; view.filter = "all"; saveView(); drawLibrary(); } }, t("Clear filters")));
}

function drawLibrary() {
  if (!root?.isConnected) return;
  els.banner.replaceChildren(...banner());
  drawChips();
  const list = data.games.filter(matches).sort(cmp[view.sort] || cmp.az);
  els.tools.hidden = !data.games.length;
  if (!list.length) els.grid.replaceChildren(emptyState());
  else if (view.sort === "system") {
    const out = [];
    let cur = null;
    for (const g of list) {
      if (g.platform !== cur) {
        cur = g.platform;
        out.push(h("h3.gm-section", platName(cur), h("span.gm-count", fmt.num(list.filter((x) => x.platform === cur).length))));
        out.push(h("div.gm-grid"));
      }
      out[out.length - 1].append(card(g));
    }
    els.grid.replaceChildren(...out);
  } else els.grid.replaceChildren(h("div.gm-grid", list.map(card)));
  redrawDetail();
}

// ---- page ---------------------------------------------------------------------------------

function drawTabs() {
  if (!els.seg) return;
  const n = activeDl().length;
  els.seg.replaceChildren(
    h("button" + (view.tab === "library" ? ".active" : ""), { onclick: () => setTab("library") }, t("Library")),
    h("button" + (view.tab === "find" ? ".active" : ""), { onclick: () => setTab("find") }, t("Find"),
      n ? h("span.gm-nbadge", fmt.num(n)) : null));
  els.libPane.hidden = view.tab !== "library";
  els.findPane.hidden = view.tab !== "find";
}

function setTab(tab) {
  view.tab = tab; saveView(); drawTabs(); schedule();
  if (tab === "find" && !fx.platforms.length) loadPlatforms();
}

export default {
  async render(el) {
    ensureCss(`/static/pages/games.css?v=${window.CASA_V}`);
    loaded = false;
    root = h("div.gm");
    els.banner = h("div");
    els.chips = h("div.gm-chips");
    els.q = h("input", { type: "search", placeholder: t("Search…"), value: query, autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), oninput: (e) => { query = e.target.value; drawLibrary(); } });
    els.grid = h("div");
    els.tools = h("div.gm-tools",
      h("div.gm-searchrow",
        h("div.gm-search", icon("search"), els.q),
        h("button.icon.gm-sortbtn", { "aria-label": t("Sort"), onclick: openSort }, sortIcon())),
      h("div.gm-chipwrap", els.chips));
    els.libPane = h("div", els.banner, els.tools, els.grid);

    els.fq = h("input", { type: "search", value: fx.q, placeholder: t("Search a game…"), autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), oninput: (e) => { fx.q = e.target.value; } });
    els.fplat = h("div.gm-chips");
    els.results = h("div");
    els.dl = h("div");
    els.findPane = h("div",
      h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); els.fq.blur(); search(true); } },
        h("div.gm-search", icon("search"), els.fq),
        h("button.primary", { type: "submit" }, t("Search"))),
      h("div.gm-chipwrap", els.fplat),
      els.dl, els.results);

    els.seg = h("div.seg.gm-seg");
    root.append(
      h("div.gm-head", h("h1", t("Games")), h("div.spacer"),
        h("button.icon.ghost", { "aria-label": t("Games settings"), onclick: openSettings }, icon("settings"))),
      els.seg, els.libPane, els.findPane);
    el.replaceChildren(root);
    drawTabs(); drawLibrary(); drawResults(); drawPlat();
    loadPlatforms();
    await Promise.all([loadLibrary(), loadDownloads()]);
    schedule();
    return () => { clearTimeout(timer); root = null; };
  },
};
