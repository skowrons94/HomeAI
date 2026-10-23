// Games: what is on the NAS, what is on the Steam Deck, one tap to install (hub/plugins/games/).
// Route: #/games. While something installs the page refreshes every 2 s, otherwise every 30 s.
import { api, h, icon, toast, ensureCss, act, fmt, t } from "../app.js";

const P = (path, opts) => api(`p/games/${path}`, opts);
const KIND = () => ({ portable: t("PC"), installer: t("PC installer"), native: t("Linux"), rom: t("Emulation") });

let data = { games: [], problems: [], deck: {} };
let filter = "all";
let query = "";
let root = null;
let timer = null;

const busyNow = () => data.games.some((g) => g.state === "installing");

async function refresh(path = "library", opts) {
  try { data = await P(path, opts); }
  catch (e) { if (root?.isConnected) toast(e.message, true); }
  draw();
  clearTimeout(timer);
  if (root?.isConnected) timer = setTimeout(() => refresh(), busyNow() ? 2000 : 30000);
}

const label = (g) => ({
  not_installed: t("Install"), installed: t("Reinstall"), update: t("Update to {v}", { v: g.version }),
  failed: t("Try again"), needs_manual: t("Download again"),
}[g.state]);

function badge(g) {
  const text = {
    installed: g.installed_version ? t("Installed · {v}", { v: g.installed_version }) : t("Installed"),
    update: t("Update available ({v})", { v: g.installed_version || "?" }),
    installing: t("Installing…"), failed: t("Failed"), needs_manual: t("Needs a manual step"),
  }[g.state];
  return text ? h(`span.gm-badge.s-${g.state}`, text) : null;
}

function card(g) {
  const online = data.deck?.online;
  const act1 = h("button" + (g.state === "not_installed" || g.state === "update" ? ".primary" : ""), {
    disabled: !online || g.state === "installing",
    title: online ? "" : t("The Steam Deck is not reachable"),
    onclick: (e) => act(async () => { await P("install", { body: { id: g.id } }); toast(t("Sent to the Steam Deck")); await refresh(); }, e.currentTarget),
  }, label(g) || t("Installing…"));
  const rm = (g.state === "installed" || g.state === "update") ? h("button.ghost", {
    onclick: (e) => confirm(t("Remove {title} from the Steam Deck?", { title: g.title })) && act(async () => {
      const r = await P("uninstall", { body: { id: g.id } });
      if (r.note) toast(r.note); await refresh();
    }, e.currentTarget),
  }, t("Remove")) : null;
  const meta = [KIND()[g.kind], g.system && g.system.toUpperCase(), g.version && `v${g.version}`, fmt.bytes(g.size)].filter(Boolean).join(" · ");
  return h("div.gm-card",
    g.cover ? h("img.gm-cover", { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, loading: "lazy", alt: "" })
            : h("div.gm-cover.none", icon("play")),
    h("div.gm-body",
      h("div.gm-title", g.title),
      h("div.gm-meta", meta),
      badge(g),
      g.state === "installing" ? h("div.progress", h("div", { style: { width: `${Math.round((g.progress || 0) * 100)}%` } })) : null,
      g.message && g.state !== "installing" ? h("div.gm-note", g.message) : (g.note ? h("div.gm-note", g.note) : null),
      h("div.gm-actions", act1, rm)));
}

function banner() {
  const d = data.deck || {};
  const out = [];
  if (!d.online) out.push(h("div.gm-banner.bad", icon("wifi"), d.error || t("The Steam Deck is not reachable")));
  if (d.online && d.pending) {
    out.push(h("div.gm-banner",
      h("span", t("{n} waiting to be added to Steam. This closes and reopens Steam; use Desktop Mode.", { n: d.pending })),
      h("button.primary", { onclick: (e) => confirm(t("Close and reopen Steam on the Deck now?")) && act(async () => {
        const r = await P("apply", { body: { restart: !!d.steam_running } });
        toast(t("Added to Steam")); await refresh();
      }, e.currentTarget) }, t("Apply to Steam"))));
  }
  return out;
}

function draw() {
  if (!root?.isConnected) return;
  const box = root.querySelector("input[type=search]");
  const focus = box && document.activeElement === box ? box.selectionStart : -1;
  const q = query.trim().toLowerCase();
  const list = data.games.filter((g) => (filter === "all" || (filter === "rom" ? g.kind === "rom" : g.kind !== "rom"))
    && (!q || g.title.toLowerCase().includes(q)));
  const tabs = [["all", t("All")], ["pc", t("PC")], ["rom", t("Emulation")]];
  const problems = data.problems?.length ? h("details.gm-problems",
    h("summary", t("{n} items skipped", { n: data.problems.length })),
    ...data.problems.map((p) => h("div.gm-note", `${p.path}: ${p.reason}`))) : null;
  const empty = !data.games.length ? h("div.empty", t("No games found in {path}", { path: data.root || "" }),
    h("p.muted", t("Put games in pc/ and ROMs in emu/<system>/ (see docs/games.md)."))) : null;
  root.replaceChildren(
    h("div.gm-head", h("h1", t("Games")),
      h("button.icon.ghost", { "aria-label": t("Rescan"), onclick: (e) => act(() => refresh("rescan", { method: "POST", body: {} }), e.currentTarget) }, icon("refresh"))),
    ...banner(),
    h("div.gm-tools",
      h("input", { type: "search", placeholder: t("Search…"), value: query, oninput: (e) => {
        query = e.target.value; draw(); } }),
      h("div.gm-tabs", tabs.map(([k, l]) => h("button" + (filter === k ? ".primary" : ""), { onclick: () => { filter = k; draw(); } }, l)))),
    h("div.gm-grid", list.length ? list.map(card) : (empty ? [empty] : [h("div.empty", t("No matches."))])),
    problems);
  if (focus >= 0) {   // typing or a background refresh must not steal the keyboard
    const again = root.querySelector("input[type=search]");
    again.focus(); again.setSelectionRange(focus, focus);
  }
}

export default {
  async render(el) {
    ensureCss(`/static/pages/games.css?v=${window.CASA_V}`);
    root = h("div.gm");
    el.replaceChildren(root);
    draw();
    await refresh();
  },
};
