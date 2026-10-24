// Torrent search: one box over the providers enabled in Settings (hub/plugins/torrents/).
// Route: #/torrents. Results are normalised by the hub; filters and sorting run server-side.
import { api, h, icon, toast, ensureCss, fmt, t, sheet, act } from "../app.js";

const state = { q: "", category: "", sort: "relevance", order: "desc", minSeeders: 0, maxGb: 0, page: 1, results: [], ids: new Set() };
let seq = 0;          // ignore answers of superseded searches
let els = {};

const qs = () => {
  const p = new URLSearchParams({ q: state.q, sort: state.sort, order: state.order, page: state.page });
  if (state.category) p.set("category", state.category);
  if (state.minSeeders > 0) p.set("min_seeders", state.minSeeders);
  if (state.maxGb > 0) p.set("max_size", Math.round(state.maxGb * 1024 ** 3));
  return p.toString();
};

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast(t("Copied")); }
  catch { toast(t("Could not copy"), true); }
}

function card(r) {
  const meta = [
    r.size != null ? fmt.bytes(r.size) : null,
    r.seeders != null ? `▲ ${r.seeders}` : null,
    r.leechers != null ? `▼ ${r.leechers}` : null,
    (r.sources || [r.source]).filter(Boolean).join(", "),
  ].filter(Boolean).join(" · ");
  return h("div.tr-card",
    h("div.tr-title", r.title),
    h("div.tr-meta", meta),
    h("div.tr-actions",
      h("button.primary", { onclick: () => downloadSheet(r) }, icon("download"), t("Download")),
      r.magnet ? h("button", { onclick: () => copy(r.magnet) }, t("Copy magnet")) : null,
      r.magnet ? h("a.btn", { href: r.magnet }, t("Open magnet")) : null,
      r.url ? h("a.btn", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, t("Torrent file")) : null));
}

function status(list) {
  const bad = (list || []).filter((p) => !p.ok);
  els.status.replaceChildren(...bad.map((p) => h("div.tr-warn", `${p.name}: ${p.error}`)));
}

// "Searching: A, B" line; after a search each tracker shows its result count (or a warning mark).
function searched(list) {
  if (!list) { els.searched.replaceChildren(); return; }
  els.searched.replaceChildren(h("span.muted", t("Searching in:")),
    ...(list.length ? list.map((p) => h(`span.chip${p.ok === false ? ".bad" : p.ok ? ".ok" : ""}`,
      p.count != null ? `${p.name} · ${p.count}` : p.name)) : [h("span.muted", t("no tracker enabled"))]));
}

async function loadEnabled() {
  try { searched(await api("p/torrents/providers")); } catch { searched(null); }
}

// ------------------------------------------------------------------ trackers menu

const KIND = { builtin: "Built-in", media: "Built-in", torznab: "Torznab", file: "File" };

function healthText(hl) {
  if (!hl) return { text: t("Not tested"), cls: "muted" };
  if (hl.ok) return { text: t("OK · {ms} ms · {n} results", { ms: hl.ms ?? "?", n: hl.count ?? 0 }), cls: "ok" };
  return { text: hl.error || t("Failed"), cls: "bad" };
}

function trackerMenu() {
  let list = [];
  let edit = null;                                   // tracker being edited, or {} for a new one
  const box = h("div.stack");
  const sh = sheet(t("Trackers"), [
    h("p.muted", t("Sites searched by the Torrents page. Turn them on or off here; no restart needed.")), box],
    { onClose: loadEnabled });

  async function reload() {
    try { list = (await api("p/torrents/trackers")).trackers; } catch (e) { toast(e.message, true); }
    draw();
  }

  async function patch(tr, body, sw) {
    try { Object.assign(tr, await api(`p/torrents/trackers/${tr.id}`, { method: "PUT", body })); }
    catch (e) { toast(e.message, true); if (sw) sw.checked = !sw.checked; }
  }

  async function test(tr, out, btn) {
    btn.disabled = true; out.textContent = t("Testing…"); out.className = "tr-health muted";
    try {
      const r = await api(`p/torrents/trackers/${tr.id}/test`, { method: "POST", body: {} });
      tr.health = { ok: r.ok, ms: r.ms, count: r.count, error: r.error };
    } catch (e) { tr.health = { ok: false, error: e.message }; }
    const hh = healthText(tr.health);
    out.textContent = hh.text; out.className = `tr-health ${hh.cls}`; btn.disabled = false;
  }

  async function remove(tr) {
    if (!confirm(t("Delete {name}?", { name: tr.name }))) return;
    try { await api(`p/torrents/trackers/${tr.id}`, { method: "DELETE" }); await reload(); }
    catch (e) { toast(e.message, true); }
  }

  function row(tr) {
    const hh = healthText(tr.health);
    const out = h(`div.tr-health.${hh.cls}`, hh.text);
    const swInput = h("input", { type: "checkbox", role: "switch", checked: tr.enabled,
      "aria-label": t("Enabled"), onchange: (e) => patch(tr, { enabled: e.target.checked }, e.target) });
    const testBtn = h("button", { onclick: () => test(tr, out, testBtn) }, t("Test"));
    return h("div.tr-card.tr-trk",
      h("div.row",
        h("div.tr-trk-name", h("div.tr-title", tr.name),
          h("div.row.wrap", h(`span.chip${tr.kind === "torznab" ? ".accent" : ""}`, t(KIND[tr.kind] || "File")),
            tr.url ? h("span.muted.tr-url", tr.url) : null)),
        h("div.spacer"), h("label.sw", swInput, h("span"))),
      out,
      h("div.tr-actions", testBtn,
        tr.editable ? h("button", { onclick: () => { edit = tr; draw(); } }, t("Edit")) : null,
        tr.editable ? h("button.danger", { onclick: () => remove(tr) }, icon("trash"), t("Delete")) : null));
  }

  function form() {
    const cur = edit.id ? edit : {};
    const name = h("input", { value: cur.name || "", maxlength: 40, placeholder: t("My Jackett"), autocomplete: "off" });
    const url = h("input", { type: "url", value: cur.url || "", inputmode: "url", autocapitalize: "off",
      placeholder: "http://jackett.local:9117/api/v2.0/indexers/all/results/torznab/" });
    const key = h("input", { type: "password", autocomplete: "new-password",
      placeholder: cur.has_key ? t("Saved. Leave empty to keep it") : t("API key") });
    const msg = h("div.tr-health.muted");
    const body = () => ({ name: name.value.trim(), url: url.value.trim(), key: key.value.trim() });
    const valid = () => {
      if (!/^https?:\/\/\S+$/i.test(url.value.trim())) { msg.textContent = t("The URL must start with http:// or https://"); msg.className = "tr-health bad"; return false; }
      if (!name.value.trim() && !cur.id) { msg.textContent = t("Give it a name"); msg.className = "tr-health bad"; return false; }
      return true;
    };
    const testBtn = h("button", { type: "button", onclick: async () => {
      if (!valid()) return;
      testBtn.disabled = true; msg.textContent = t("Testing…"); msg.className = "tr-health muted";
      try {
        const r = await api("p/torrents/trackers/test", { body: { ...body(), id: cur.id } });
        msg.className = `tr-health ${r.ok ? "ok" : "bad"}`;
        msg.textContent = r.ok ? t("OK · {ms} ms · {n} results", { ms: r.ms ?? "?", n: r.count }) : r.error;
      } catch (e) { msg.className = "tr-health bad"; msg.textContent = e.message; }
      testBtn.disabled = false;
    } }, t("Test"));
    return h("form.tr-card.stack", { onsubmit: async (e) => {
      e.preventDefault();
      if (!valid()) return;
      try {
        if (cur.id) await api(`p/torrents/trackers/${cur.id}`, { method: "PUT", body: body() });
        else await api("p/torrents/trackers", { body: body() });
        edit = null; await reload();
      } catch (err) { msg.className = "tr-health bad"; msg.textContent = err.message; }
    } },
      h("h3", cur.id ? t("Edit tracker") : t("Add tracker")),
      h("label.tr-field", t("Name"), name),
      h("label.tr-field", t("Torznab URL"), url),
      h("label.tr-field", t("API key"), key),
      h("div.muted.tr-hint", t("Jackett, Prowlarr or any Torznab endpoint.")),
      msg,
      h("div.tr-actions", h("button.primary", { type: "submit" }, t("Save")), testBtn,
        h("button", { type: "button", onclick: () => { edit = null; draw(); } }, t("Cancel"))));
  }

  function draw() {
    box.replaceChildren(...list.map(row),
      edit ? form() : h("button.primary.tr-add", { onclick: () => { edit = {}; draw(); } }, icon("plus"), t("Add tracker")));
  }
  reload();
}

function draw({ loading = false, more = false, error = "" } = {}) {
  const body = [];
  if (error) body.push(h("div.tr-error", { role: "alert" }, icon("alert"), h("span", error)));
  body.push(...state.results.map(card));
  if (loading) body.push(h("div.empty", t("Searching…")));
  else if (!state.results.length && state.q && !error) body.push(h("div.empty", t("No results.")));
  if (!loading && more) body.push(h("button.tr-more", { onclick: () => run(true) }, t("Load more")));
  els.list.replaceChildren(...body);
  els.go.disabled = loading;
}

async function run(append = false) {
  const mine = ++seq;
  if (!append) { state.page = 1; state.results = []; state.ids = new Set(); } else state.page += 1;
  if (state.q.trim().length < 2) { draw({ error: t("Type at least 2 characters") }); return; }
  draw({ loading: true });
  try {
    const d = await api(`p/torrents/search?${qs()}`);
    if (mine !== seq) return;
    for (const r of d.results) if (!state.ids.has(r.id)) { state.ids.add(r.id); state.results.push(r); }
    status(d.providers);
    searched(d.providers);
    draw({ more: d.has_more, error: d.error || "" });
  } catch (e) {
    if (mine !== seq) return;
    if (append) state.page -= 1;
    draw({ error: e.message });
  }
}

// -- one Download button for every library ---------------------------------------------------
// The hub guesses film / series / music / game from the name; the user confirms or corrects it,
// then the owning plugin downloads it with Transmission and files it in its library folder.

const KIND_LABEL = () => ({ movie: t("Film"), series: t("Series"), music: t("Music"), game: t("Game") });
let gamePlatforms = null;   // [[key, label]] from the Games plugin: one list for every page
async function platformsList() {
  if (!gamePlatforms) {
    try { gamePlatforms = (await api("p/games/find/platforms")).map((p) => [p.key, p.label]); }
    catch { gamePlatforms = [["pc", "PC"]]; }
  }
  return gamePlatforms;
}

function downloadSheet(r) {
  const st = { kind: null, guess: {}, candidates: [], pick: null, available: {}, query: "" };
  const s = sheet(t("Download"), h("div.muted", t("Working out what it is…")));
  const field = (label, value, set, attrs = {}) => h("label.tr-field", label,
    h("input", { value: value ?? "", oninput: (e) => set(e.target.value), ...attrs }));

  async function load(kind, query) {
    const d = await api("p/torrents/identify", { body: { title: r.title, kind, query } });
    Object.assign(st, { kind: d.kind, guess: d.guess || {}, candidates: d.candidates || [], available: d.available || {} });
    if (st.kind === "game") st.platforms = await platformsList();
    st.pick = st.candidates[0] || null;
    st.query = st.guess.query || st.query;
    draw();
  }

  function dest() {
    const g = st.guess;
    if (st.kind === "movie") return st.pick ? `movies/${st.pick.title}${st.pick.year ? ` (${st.pick.year})` : ""}` : "movies/";
    if (st.kind === "series") return st.pick ? `series/${st.pick.title}` : "series/";
    if (st.kind === "game") return g.platform && g.platform !== "pc" ? `games/emu/${g.platform}` : "games/pc";
    return `music/${g.artist || "?"}${g.album ? `/${g.album}${g.year ? ` (${g.year})` : ""}` : ""}`;
  }

  function details() {
    const g = st.guess;
    if (st.kind === "movie" || st.kind === "series") {
      const search = h("form.row.tr-idsearch", { onsubmit: (e) => { e.preventDefault(); act(() => load(st.kind, st.query)); } },
        h("input", { type: "search", value: st.query, enterkeyhint: "search", placeholder: t("Title"),
                     oninput: (e) => { st.query = e.target.value; } }),
        h("button", { type: "submit", "aria-label": t("Search") }, icon("search")));
      const list = st.candidates.length
        ? h("div.tr-cands", st.candidates.map((c) => h(`button.tr-cand${st.pick?.id === c.id ? ".on" : ""}`,
            { onclick: () => { st.pick = c; draw(); } },
            c.poster ? h("img", { src: c.poster, alt: "", loading: "lazy" }) : h("div.tr-noposter", icon("film")),
            h("div.tr-cand-t", h("b", c.title), h("span.muted", c.year || "")))))
        : h("p.muted", t("Nothing found: try another title."));
      const se = st.kind === "series" ? h("div.row.wrap",
        field(t("Season"), g.season, (v) => { g.season = v === "" ? null : +v; }, { type: "number", min: 0, inputmode: "numeric" }),
        field(t("Episode"), g.episode, (v) => { g.episode = v === "" ? null : +v; }, { type: "number", min: 0, inputmode: "numeric" }))
        : null;
      return [search, list, se];
    }
    if (st.kind === "game") return [
      field(t("Title"), g.title, (v) => { g.title = v; }),
      h("label.tr-field", t("Platform"), h("select", { onchange: (e) => { g.platform = e.target.value; draw(); } },
        (st.platforms || [["pc", "PC"]]).map(([k, l]) => h("option", { value: k, selected: (g.platform || "pc") === k }, l))))];
    return [
      field(t("Artist"), g.artist, (v) => { g.artist = v; draw(false); }),
      field(t("Album"), g.album, (v) => { g.album = v; draw(false); }),
      field(t("Year"), g.year, (v) => { g.year = v ? +v : null; draw(false); }, { type: "number", inputmode: "numeric" })];
  }

  let destEl;
  function draw(full = true) {
    if (!full && destEl) { destEl.textContent = dest(); return; }
    const labels = KIND_LABEL();
    const seg = h("div.seg.tr-kinds", ["movie", "series", "music", "game"].map((k) =>
      h(`button${st.kind === k ? ".active" : ""}`, { type: "button", disabled: st.available[k] === false,
        onclick: () => k !== st.kind && act(() => load(k)) }, labels[k])));
    destEl = h("b", dest());
    const missing = st.available[st.kind] === false;
    const ok = !missing && ((st.kind !== "movie" && st.kind !== "series") || st.pick);
    const go = h("button.primary.tr-go", { disabled: !ok, onclick: (e) => act(async () => {
      const guess = st.kind === "movie" || st.kind === "series"
        ? { id: st.pick.id, season: st.guess.season ?? null, episode: st.guess.episode ?? null }
        : st.guess;
      await api("p/torrents/download", { body: { result: r, kind: st.kind, guess } });
      toast(t("Downloading: {what}", { what: dest() }));
      s.close();
      loadDownloads();
    }, e.currentTarget) }, icon("download"), t("Download"));
    s.el.replaceChildren(
      h("div.tr-title", r.title),
      h("div.tr-meta", [r.size != null ? fmt.bytes(r.size) : null, r.seeders != null ? `▲ ${r.seeders}` : null]
        .filter(Boolean).join(" · ")),
      seg,
      missing ? h("p.tr-warn", t("Turn on the {name} plugin to download this", { name: t(st.kind === "game" ? "Games" : st.kind === "music" ? "Music" : "Media") })) : null,
      ...details(),
      h("p.tr-dest", t("It will go to"), " ", destEl),
      go);
  }

  act(() => load()).then(() => { if (!st.kind) s.close(); });
}

// downloads started from any page, shown on top while something is in progress
let dlTimer = null;
async function loadDownloads() {
  clearTimeout(dlTimer);
  if (!els.downloads?.isConnected) return;
  let list = [];
  try { list = await api("p/torrents/downloads"); } catch { list = []; }
  const labels = KIND_LABEL();
  els.downloads.replaceChildren(...(list.length ? [
    h("h3.tr-h", t("In progress")),
    h("div.tr-dl", list.map((d) => h("a.tr-dlrow", { href: d.page || "#/torrents" },
      h("div.tr-dl-top", h("span.chip", labels[d.kind] || d.kind), h("span.tr-dl-t", d.title || d.name)),
      h("div.tr-bar", h("i", { style: `width:${Math.round((d.progress || 0) * 100)}%` })),
      h(`div.tr-dl-s${d.error ? ".bad" : ""}`, d.error || `${Math.round((d.progress || 0) * 100)}% · ${d.state || ""}`))))] : []));
  if (list.length) dlTimer = setTimeout(loadDownloads, 4000);
}

const option = (value, label, cur) => h("option", { value, selected: value === cur }, label);

export default {
  async render(root) {
    ensureCss(`/static/pages/torrents.css?v=${window.CASA_V}`);
    const input = h("input", { type: "search", placeholder: t("Search…"), value: state.q, maxlength: 100,
                               enterkeyhint: "search", oninput: (e) => { state.q = e.target.value; } });
    els = { go: h("button.primary", { type: "submit" }, icon("search"), t("Search")),
            status: h("div.tr-status"), downloads: h("div.tr-downloads"), searched: h("div.row.wrap.tr-searched"), list: h("div.tr-list") };
    const again = () => state.q && run();
    const CATS = [["", t("All")], ["movie", t("Films")], ["series", t("Series")], ["music", t("Music")], ["games", t("Games")]];
    const cats = h("div.tr-cats");
    const paintCats = () => cats.replaceChildren(
      ...CATS.map(([k, label]) => h(`button.chip${k === state.category ? ".on" : ""}`, { type: "button",
        onclick: () => { state.category = k; paintCats(); again(); } }, label)),
      h("div.spacer"),
      h(`button.chip.tr-ftoggle${state.showFilters ? ".on" : ""}`, { type: "button", "aria-expanded": String(!!state.showFilters),
        title: t("Filters"), "aria-label": t("Filters"),
        onclick: () => { state.showFilters = !state.showFilters; root.querySelector(".tr-filters").hidden = !state.showFilters; paintCats(); } },
        icon("settings")));
    paintCats();
    root.replaceChildren(
      h("div.row.tr-head", h("h1", t("Torrents")), h("div.spacer"),
        h("button", { onclick: trackerMenu }, icon("settings"), t("Trackers"))),
      h("form.tr-form", { onsubmit: (e) => { e.preventDefault(); run(); } }, input, els.go),
      cats,
      h("div.tr-filters", { hidden: !state.showFilters },
        h("label", t("Sort"), h("select", { onchange: (e) => { state.sort = e.target.value; again(); } },
          option("relevance", t("Relevance"), state.sort), option("seeders", t("Seeders"), state.sort),
          option("size", t("Size"), state.sort))),
        h("label", t("Order"), h("select", { onchange: (e) => { state.order = e.target.value; again(); } },
          option("desc", t("Descending"), state.order), option("asc", t("Ascending"), state.order))),
        h("label", t("Min seeders"), h("input", { type: "number", min: 0, value: state.minSeeders,
          onchange: (e) => { state.minSeeders = Math.max(0, +e.target.value || 0); again(); } })),
        h("label", t("Max size (GB)"), h("input", { type: "number", min: 0, step: "0.5", value: state.maxGb,
          onchange: (e) => { state.maxGb = Math.max(0, +e.target.value || 0); again(); } }))),
      els.downloads, els.searched, els.status, els.list);
    draw();
    loadEnabled();
    loadDownloads();
  },
};
