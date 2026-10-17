// Media: discover, search, favourites, downloads, library (hub/plugins/media).
// Routes: #/media[/discover|watchlist|downloads|library|search/<q>|movie/<id>|series/<id>]
import { api, h, icon, toast, sheet, ensureCss, act, debounce, fmt, bus, go } from "../app.js";

const P = (path, opts) => api(`p/media/${path}`, opts);
const FLAG = { it: "🇮🇹", en: "🇬🇧", fr: "🇫🇷", es: "🇪🇸", de: "🇩🇪", ru: "🇷🇺", uk: "🇺🇦", ko: "🇰🇷",
               ja: "🇯🇵", hi: "🇮🇳", pt: "🇵🇹", pl: "🇵🇱", zh: "🇨🇳", nl: "🇳🇱" };
const TABS = [["discover", "Discover"], ["watchlist", "Favourites"], ["downloads", "Downloads"], ["library", "Library"]];
const enc = encodeURIComponent;
const itemHash = (it) => `#/media/${it.type}/${enc(it.id)}`;
// replaceChildren() would print "null" for skipped optional children
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const flags = (langs) => (langs || []).map((l) => FLAG[l] || l.toUpperCase()).join(" ");

let status = null;  // plugin status, refreshed on page load

export default {
  async render(root, args) {
    ensureCss(`/static/pages/media.css?v=${window.CASA_V}`);
    const [view = "discover", ...rest] = args;
    const off = [];
    const on = (fn) => { bus.addEventListener("media", fn); off.push(() => bus.removeEventListener("media", fn)); };
    const cleanup = () => off.forEach((f) => f());

    if (view === "movie" || view === "series") {
      await renderDetail(root, view, rest.join("/"), on);
      return cleanup;
    }

    // header: title, search, tabs
    const results = h("div");
    const search = h("input", { type: "search", placeholder: "Search movies & series…",
                                value: view === "search" ? rest.join("/") : "", enterkeyhint: "search" });
    const tabs = h("div.tabs", TABS.map(([id, label]) =>
      h(`button${view === id ? ".active" : ""}`, { onclick: () => go(`#/media/${id}`) }, label)));
    root.append(
      h("div.m-head", h("h1", "Media"),
        h("div.m-search", icon("search"), search)),
      tabs, h("div#m-warn"), results);
    const doSearch = debounce(async (q) => {
      if (!q.trim()) { if (view === "search") go("#/media/discover"); return; }
      history.replaceState(null, "", `#/media/search/${enc(q)}`);
      tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      await renderSearch(results, q);
    }, 450);
    search.addEventListener("input", () => doSearch(search.value));

    P("status").then((s) => { status = s; warnings(root.querySelector("#m-warn"), s); }).catch(() => {});

    if (view === "search") await renderSearch(results, rest.join("/"));
    else if (view === "watchlist") await renderWatchlist(results, on);
    else if (view === "downloads") await renderDownloads(results, on);
    else if (view === "library") await renderLibrary(results);
    else await renderDiscover(results);
    return cleanup;
  },
};

// ---------------------------------------------------------------- shared bits

function warnings(el, s) {
  const out = [];
  if (!s.tmdb) out.push(h("div.m-banner", "🎬",
    h("div", h("b", "Using Cinemeta (English titles). "),
      "For Italian titles, trends and upcoming releases, add a free TMDB key: themoviedb.org → Settings → API, then ",
      h("code", "tmdb_api_key"), " under [plugins.media] in config.toml.")));
  if (s.transmission && s.transmission.ok === false) out.push(h("div.m-banner", "⚠️",
    h("div", h("b", "Transmission offline. "), s.transmission.error, " - downloads can't start.")));
  fill(el, ...out);
}

function poster(it, clickable = true) {
  const img = it.poster ? h("img", { src: it.poster, loading: "lazy", alt: "" }) : h("div.ph", it.title);
  return h("div.poster", { onclick: clickable ? () => go(itemHash(it)) : null, title: it.title,
                          style: clickable ? null : { cursor: "default" } },
    h("div.img", img,
      h("div.badges",
        it.in_library && h("span.badge.lib", { title: "In your library" }, icon("check")),
        it.favourite && h("span.badge.fav", { title: "Favourite" }, icon("heart"))),
      it.rating && h("span.rating", `★ ${it.rating}`)),
    h("div.t", it.title),
    h("div.y", [it.year, it.type === "series" ? "Series" : ""].filter(Boolean).join(" · ")));
}

const loading = () => h("div.empty", h("div.spin"));

// ---------------------------------------------------------------- discover / search

async function renderDiscover(el) {
  fill(el, loading());
  const d = await P("discover");
  fill(el, ...d.sections.filter((s) => s.items.length).map((s) => [
    h("div.section", h("h2", s.title)),
    h("div.m-row", s.items.map(poster)),
  ]).flat());
  if (!d.sections.length) fill(el, h("div.empty", "Couldn't reach the movie database."));
}

async function renderSearch(el, q) {
  fill(el, loading());
  const items = await P(`search?q=${enc(q)}`);
  fill(el, 
    h("div.section", h("h2", `Results for “${q}”`), h("span.muted", items.length)),
    items.length ? h("div.m-grid", items.map(poster)) : h("div.empty", "Nothing found."));
}

// ---------------------------------------------------------------- detail

async function renderDetail(root, type, id, on) {
  fill(root, loading());
  const it = await P(`details/${type}/${enc(id)}`);
  const body = h("div.detail-body");
  fill(root, 
    h("div.hero", { style: { backgroundImage: it.backdrop ? `url("${it.backdrop}")` : "" } },
      h("button.icon.back", { onclick: () => history.length > 1 ? history.back() : go("#/media") }, icon("left")),
      h("div.inner",
        it.poster ? h("img.pimg", { src: it.poster, alt: "" }) : h("div.pimg"),
        h("div.stack", { style: { gap: "6px", minWidth: 0 } },
          h("h1", it.title),
          h("div.row.wrap.small.muted",
            [it.year, typeof it.runtime === "number" ? `${it.runtime} min` : it.runtime, it.type === "series" ? "Series" : "Movie",
             ...(it.genres || []).slice(0, 3)].filter(Boolean).join(" · "),
            it.rating && h("span.chip", `★ ${it.rating}`),
            it.in_library && h("span.chip.ok", icon("check"), "In library"))))),
    body);

  const favBtn = h("button", { onclick: () => toggleFav() });
  const autoSw = h("input", { type: "checkbox" });
  const favRow = h("div.row.small");
  const paint = () => {
    const w = it.watch;
    fill(favBtn, icon("heart"), w ? "Favourite" : "Add to favourites");
    favBtn.className = w ? "on" : "";
    fill(favRow, ...(w ? [
      h("label.row", { style: { gap: "6px" } }, autoSw, "Download automatically when found"),
      w.note && h("span.chip.accent", w.note)] : [
      h("span.muted", "Favourites are checked every few hours; you get a notification when a good release shows up.")]));
    autoSw.checked = !!w?.auto_download;
  };
  async function toggleFav() {
    await act(async () => {
      if (it.watch) { await P(`watchlist/${enc(`${type}:${it.id}`)}`, { method: "DELETE" }); it.watch = null; toast("Removed from favourites"); }
      else { it.watch = await P("watchlist", { body: { type, id: it.id } }); toast("❤️ Added - I'll keep an eye on it"); }
      paint();
    }, favBtn);
  }
  autoSw.onchange = () => act(async () => {
    it.watch = await P(`watchlist/${enc(`${type}:${it.id}`)}`, { method: "PATCH", body: { auto_download: autoSw.checked } });
    paint();
  });
  paint();

  const actions = h("div.row.wrap", favBtn,
    type === "movie" && !it.in_library && h("button.primary", { onclick: (e) => quickDownload(it, {}, e.currentTarget) },
      icon("download"), "Download best"),
    type === "movie" && h("button", { onclick: () => openReleases(it, {}) }, icon("search"), "Find releases"),
    it.trailer && h("a.btn", { href: it.trailer, target: "_blank", rel: "noopener" }, icon("play"), "Trailer"));

  const dlBox = h("div");
  const paintDl = (items) => {
    const mine = items.filter((d) => d.key === `${type}:${it.id}` && !["imported", "removed"].includes(d.state));
    fill(dlBox, ...(mine.length ? [h("div.list", mine.map(dlRow))] : []));
  };
  paintDl(it.downloads || []);
  on((e) => e.detail.kind === "downloads" && paintDl(e.detail.items));

  body.append(actions, favRow, dlBox,
    it.overview && h("p.overview", it.overview),
    it.cast?.length && h("div.small.muted", "With ", it.cast.slice(0, 6).join(", ")));

  if (type === "series") body.append(await seasonsView(it, on));
}

async function seasonsView(it, on) {
  const seasons = it.seasons || [];
  if (!seasons.length) return h("div.empty", "No season information.");
  const wrap = h("div.stack");
  const list = h("div");
  const today = new Date().toISOString().slice(0, 10);
  let current = [...seasons].reverse().find((s) => (s.air_date || "9") <= today)?.season ?? seasons[0].season;
  const seg = h("div.tabs");
  const paintSeg = () => fill(seg, ...seasons.map((s) =>
    h(`button${s.season === current ? ".active" : ""}`, { onclick: () => { current = s.season; paintSeg(); load(); } },
      `S${s.season}`, s.in_library ? h("span.small", { style: { opacity: .7 } }, ` ${s.in_library}/${s.episodes ?? "?"}`) : null)));
  async function load() {
    fill(list, loading());
    const eps = await P(`episodes/series/${enc(it.id)}/${current}`);
    const missing = eps.filter((e) => e.aired && !e.in_library && !e.downloading);
    fill(list, 
      h("div.row.wrap", { style: { margin: "10px 0" } },
        h("b", `Season ${current}`), h("span.muted.small", `${eps.filter((e) => e.in_library).length}/${eps.length} in library`),
        h("div.spacer"),
        missing.length > 1 && h("button.primary", { onclick: (e) => quickDownload(it, { season: current }, e.currentTarget) },
          icon("download"), "Download season"),
        h("button", { onclick: () => openReleases(it, { season: current }) }, "Season packs")),
      h("div.list", eps.map((e) => h("div.item.ep",
        h("div.num", e.episode),
        e.still ? h("img.still", { src: e.still, loading: "lazy", alt: "" }) : null,
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("b.ellipsis", e.title || `Episode ${e.episode}`),
          h("div.small.muted", e.air_date ? new Date(e.air_date).toLocaleDateString() : "TBA")),
        e.in_library ? h("span.chip.ok", icon("check"), "Library")
          : e.downloading ? h("span.chip.accent", `${Math.round(e.downloading.progress * 100)}%`)
          : !e.aired ? h("span.chip", "Not aired")
          : [h("button.icon", { title: "Download best", onclick: (ev) => quickDownload(it, { season: current, episode: e.episode }, ev.currentTarget) }, icon("download")),
             h("button.icon.ghost", { title: "Choose release", onclick: () => openReleases(it, { season: current, episode: e.episode }) }, icon("more"))]))));
  }
  const reload = debounce(load, 1500);
  on((e) => e.detail.kind === "downloads" && e.detail.items.some((d) => d.state === "imported") && reload());
  paintSeg();
  wrap.append(h("div.section", h("h2", "Episodes")), seg, list);
  load();
  return wrap;
}

async function quickDownload(it, where, btn) {
  await act(async () => {
    const r = await P("download", { body: { type: it.type, id: it.id, ...where } });
    const rel = r.release;
    toast(`⬇ ${flags(rel.audio.slice(0, 2))} ${rel.quality} · ${fmt.bytes(rel.size)}`);
  }, btn);
}

async function openReleases(it, where) {
  const label = where.episode ? `S${String(where.season).padStart(2, "0")}E${String(where.episode).padStart(2, "0")}`
              : where.season ? `Season ${where.season}` : it.year;
  const s = sheet(`${it.title} · ${label}`, loading(), { wide: true });
  const qs = new URLSearchParams(Object.entries(where).filter(([, v]) => v != null));
  try {
    const r = await P(`releases/${it.type}/${enc(it.id)}?${qs}`);
    const ok = r.releases.filter((x) => !x.rejected);
    const bad = r.releases.filter((x) => x.rejected);
    const sources = Object.entries(r.stats).map(([k, v]) =>
      h(`span.chip${v.error ? ".bad" : ""}`, { title: v.error || "" }, `${k} ${v.error ? "✕" : v.count}`));
    const showBad = h("button.ghost.small", bad.length ? `Show ${bad.length} rejected` : "");
    const badList = h("div.list.hidden", bad.slice(0, 60).map((x) => relRow(it, where, x, false, s)));
    showBad.onclick = () => { badList.classList.toggle("hidden"); };
    fill(s.el, 
      h("div.row.wrap", ...sources),
      ok.length ? h("div.list", ok.map((x, i) => relRow(it, where, x, i === 0, s)))
                : h("div.empty", "No acceptable release yet. Add it to favourites and you'll be notified."),
      bad.length ? showBad : null, badList);
  } catch (e) {
    fill(s.el, h("div.empty", e.message));
  }
}

function relRow(it, where, x, best, s) {
  return h(`div.item.rel${best ? ".best" : ""}${x.rejected ? ".rejected" : ""}`,
    h("div.flags", { title: (x.audio || []).join(", ") }, flags((x.audio || []).slice(0, 2)) || "?"),
    h("div", { style: { flex: 1, minWidth: 0 } },
      h("div.nm", x.name),
      h("div.meta",
        best && h("span.chip.accent", "★ Best match"),
        x.quality && h("span.chip", x.quality),
        h("span.chip", fmt.bytes(x.size)),
        h(`span.chip${x.seeders > 20 ? ".ok" : x.seeders < 3 ? ".warn" : ""}`, `▲ ${x.seeders}`),
        h("span.small.muted", x.source),
        x.subs?.includes("it") && x.language !== "it" && h("span.chip", "sub ITA"),
        x.rejected && h("span.chip.bad", x.rejected))),
    !x.rejected || x.rejected.startsWith("language") || x.rejected.startsWith("too big") ?
      h(`button.icon${best ? ".primary" : ""}`, { title: "Download", onclick: (e) => act(async () => {
        await P("download", { body: { type: it.type, id: it.id, hash: x.hash, ...where } });
        toast("⬇ Sent to downloads");
        s.close();
      }, e.currentTarget) }, icon("download")) : null);
}

// ---------------------------------------------------------------- favourites

const WSTATUS = { in_library: "ok", downloading: "accent", found: "accent", up_to_date: "ok", waiting: "", upcoming: "warn" };

async function renderWatchlist(el, on) {
  const paint = async () => {
    const items = await P("watchlist");
    if (!items.length) {
      fill(el, h("div.empty", "No favourites yet. Open a movie or series and tap ♥ - you'll get a notification as soon as it's available (Italian first)."));
      return;
    }
    fill(el, h("div.section", h("h2", "Favourites"), h("span.muted", items.length)),
      h("div.list", items.map((w) => h("div.item",
        w.poster ? h("img", { src: w.poster, style: { width: "46px", height: "69px", borderRadius: "6px", objectFit: "cover" } }) : null,
        h("div.stack", { style: { gap: "4px", flex: 1, minWidth: 0, cursor: "pointer" }, onclick: () => go(`#/media/${w.type}/${enc(w.id)}`) },
          h("b.ellipsis", w.title, w.year ? h("span.muted", ` (${w.year})`) : ""),
          h("div.row.wrap", h(`span.chip${WSTATUS[w.status] ? "." + WSTATUS[w.status] : ""}`, (w.status || "new").replace(/_/g, " ")),
            h("span.small.muted", w.note || ""),
            w.auto_download && h("span.chip", icon("download"), "auto"))),
        h("button.icon.ghost", { title: "Check now", onclick: (e) => act(async () => {
          await P(`watchlist/${enc(w.key)}/check`, { body: {} }); paint(); }, e.currentTarget) }, icon("refresh")),
        h("button.icon.ghost", { title: "Remove", onclick: (e) => act(async () => {
          await P(`watchlist/${enc(w.key)}`, { method: "DELETE" }); paint(); }, e.currentTarget) }, icon("trash"))))));
  };
  fill(el, loading());
  await paint();
  on((e) => e.detail.kind === "watchlist" && paint());
}

// ---------------------------------------------------------------- downloads

const DSTATE = { downloading: "accent", queued: "", paused: "warn", completed: "accent", seeding: "ok",
                 imported: "ok", failed: "bad", removed: "" };

function dlRow(d) {
  const active = ["queued", "downloading", "paused"].includes(d.state);
  const what = d.season != null ? ` S${String(d.season).padStart(2, "0")}${d.episode != null ? "E" + String(d.episode).padStart(2, "0") : ""}` : "";
  return h("div.item.dl",
    d.data?.poster ? h("img", { src: d.data.poster, alt: "" }) : h("div.tile", h("div.ico", icon("film"))),
    h("div", { style: { flex: 1, minWidth: 0 } },
      h("div.row", h("b.ellipsis", d.title + what), h(`span.chip${DSTATE[d.state] ? "." + DSTATE[d.state] : ""}`, d.state)),
      h("div.small.muted.ellipsis", d.name),
      active && h("div.progress", { style: { marginTop: "8px" } }, h("div", { style: { width: `${(d.progress * 100).toFixed(1)}%` } })),
      h("div.row2",
        h("span", `${(d.progress * 100).toFixed(d.progress < 1 ? 1 : 0)}%`),
        d.size ? h("span", fmt.bytes(d.size)) : null,
        active && d.rate ? h("span", `${fmt.bytes(d.rate)}/s`) : null,
        active && d.eta > 0 ? h("span", `ETA ${fmt.duration(d.eta)}`) : null,
        d.error ? h("span", { style: { color: "var(--bad)" } }, d.error) : null)),
    d.state === "downloading" && h("button.icon.ghost", { title: "Pause", onclick: (e) => dlAct(d, "pause", e) }, icon("pause")),
    d.state === "paused" && h("button.icon.ghost", { title: "Resume", onclick: (e) => dlAct(d, "resume", e) }, icon("play")),
    d.state === "failed" && h("button.icon.ghost", { title: "Retry import", onclick: (e) => dlAct(d, "retry_import", e) }, icon("refresh")),
    h("button.icon.ghost", { title: active ? "Cancel & delete partial data" : "Remove from list", onclick: (e) => {
      if (!active || confirm(`Cancel ${d.title}?`)) dlAct(d, "remove", e);
    } }, icon("x")));
}

const dlAct = (d, a, e) => act(() => P(`downloads/${d.hash}/${a}`, { body: {} }), e.currentTarget);

async function renderDownloads(el, on) {
  const paint = (items) => {
    const active = items.filter((d) => ["queued", "downloading", "paused", "completed", "seeding"].includes(d.state));
    const done = items.filter((d) => !active.includes(d));
    fill(el, 
      h("div.section", h("h2", "Downloading"), h("span.muted", active.length)),
      active.length ? h("div.list", active.map(dlRow)) : h("div.empty", "Nothing downloading. Find something in Discover ✨"),
      done.length ? [h("div.section", h("h2", "Finished")), h("div.list", done.slice(0, 30).map(dlRow))] : null);
  };
  fill(el, loading());
  paint(await P("downloads"));
  on((e) => e.detail.kind === "downloads" && paint(e.detail.items));
}

// ---------------------------------------------------------------- library

async function renderLibrary(el) {
  fill(el, loading());
  let kind = "movies";
  const grid = h("div");
  const stats = h("div.grid");
  const scanBtn = h("button", { onclick: () => act(async () => { await P("library/scan", { body: {} }); toast("Library rescanned"); load(); }, scanBtn) },
    icon("refresh"), "Rescan");
  const seg = h("div.seg");
  const paintSeg = () => fill(seg, ...[["movies", "Movies"], ["series", "Series"]].map(([k, l]) =>
    h(`button${k === kind ? ".active" : ""}`, { onclick: () => { kind = k; paintSeg(); load(); } }, l)));
  async function load() {
    const r = await P(`library?kind=${kind}`);
    const s = r.stats;
    fill(stats, 
      h("div.card.stat", h("b", s.movies), h("span.muted.small", "movies")),
      h("div.card.stat", h("b", s.shows), h("span.muted.small", "series")),
      h("div.card.stat", h("b", s.episodes), h("span.muted.small", "episodes")),
      h("div.card.stat", h("b", fmt.bytes(s.size)), h("span.muted.small", s.last_scan ? `scanned ${fmt.ago(s.last_scan)}` : "never scanned")));
    const missing = [!s.movies_dir_ok && `movies folder not found: ${s.movies_dir || "(not set)"}`,
                     !s.series_dir_ok && `series folder not found: ${s.series_dir || "(not set)"}`].filter(Boolean);
    fill(grid, 
      missing.length ? h("div.m-banner", "📁", h("div", missing.join(" · "), " - is the NAS mounted?")) : null,
      r.items.length ? h("div.m-grid", r.items.map((x) => poster({
        ...x, type: kind === "movies" ? "movie" : "series", id: x.meta_id,
        in_library: false, title: x.title, year: [x.year, x.episodes ? `${x.episodes} ep` : x.quality].filter(Boolean).join(" · "),
      }, !!x.meta_id))) : h("div.empty", "Empty."));
  }
  fill(el, 
    h("div.section", h("h2", "Your library"), h("div.spacer"), scanBtn), stats,
    h("div.section", seg), grid,
    h("div.section", h("h2", "Sources")), await sourcesPanel());
  paintSeg();
  await load();
}

async function sourcesPanel() {
  const box = h("div.stack");
  const paint = (info) => {
    const hl = info.health || {};
    fill(box, 
      h("div.list",
        h("div.item", icon("link"), h("div", { style: { flex: 1 } }, h("b", "Proxy"),
          h("div.small.muted", info.proxy || "none - set [plugins.media.proxy] url = \"socks5h://…\" to reach blocked trackers")),
          info.error && h("span.chip.bad", info.error)),
        info.enabled.map((n) => {
          const x = hl[n] || {};
          return h("div.item",
            h(`span.chip${x.ok ? ".ok" : x.ok === false ? ".bad" : ""}`, x.ok ? "online" : x.ok === false ? "offline" : "…"),
            h("div", { style: { flex: 1, minWidth: 0 } }, h("b", n),
              h("div.small.muted.ellipsis", x.ok ? `${x.mirror} · ${x.latency_ms} ms` : x.error || "not checked")));
        })),
      h("div.row", h("span.small.muted", status?.transmission ? `Transmission: ${status.transmission.ok ? "connected" : status.transmission.error || "?"}` : ""),
        h("div.spacer"), testBtn));
  };
  const testBtn = h("button", { onclick: () => act(async () => paint(await P("indexers/test", { body: {} })), testBtn) },
    icon("refresh"), "Test sources");
  paint(await P("indexers"));
  return box;
}
