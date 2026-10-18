// Media: discover, search, favourites, downloads, library (hub/plugins/media).
// Routes: #/media[/discover|watchlist|downloads|library|search/<q>|movie/<id>|series/<id>]
import { t, api, h, icon, toast, sheet, ensureCss, act, debounce, fmt, bus, go, hasPlugin } from "../app.js";

const P = (path, opts) => api(`p/media/${path}`, opts);
const FLAG = { it: "🇮🇹", en: "🇬🇧", fr: "🇫🇷", es: "🇪🇸", de: "🇩🇪", ru: "🇷🇺", uk: "🇺🇦", ko: "🇰🇷",
               ja: "🇯🇵", hi: "🇮🇳", pt: "🇵🇹", pl: "🇵🇱", zh: "🇨🇳", nl: "🇳🇱" };
const tabList = () => [["discover", t("Discover")], ["watchlist", t("To watch")], ["downloads", t("Coming in")], ["library", t("Library")]];
const enc = encodeURIComponent;
// Cinemeta genres are English data (TMDB ones come already translated): namespaced keys,
// since e.g. "History" is also a page title
const genreName = (n) => { const k = `genre: ${n}`, v = t(k); return v === k ? n : v; };
const itemHash = (it) => `#/media/${it.type}/${enc(it.id)}`;
// replaceChildren() would print "null" for skipped optional children
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
// crude: is this plot English? (Cinemeta has no Italian text; we don't translate, we say so)
const looksEnglish = (txt) => {
  const w = ` ${String(txt).toLowerCase().replace(/[^a-zàèéìòù ]/g, " ")} `;
  const n = (list) => list.reduce((a, x) => a + (w.split(` ${x} `).length - 1), 0);
  return n(["the", "and", "his", "her", "with", "who", "their", "that", "into"]) > n(["il", "la", "che", "un", "una", "di", "con", "per", "della", "gli"]);
};
const INFUSE = "infuse://";
const infuseLink = () => h("a.btn.ghost", { href: INFUSE }, t("Open in Infuse"));
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
      try { await renderDetail(root, view, rest.join("/"), on); }
      catch (e) {
        toast(t("Couldn't load details: {error}", { error: e.message }), true);
        fill(root, h("button", { onclick: () => go("#/media") }, icon("left"), t("Back")),
             h("div.empty", e.message));
      }
      return cleanup;
    }

    // header: title, search, tabs
    const results = h("div");
    const search = h("input", { type: "search", placeholder: t("Search movies & series…"),
                                value: view === "search" ? rest.join("/") : "", enterkeyhint: "search" });
    const tabs = h("div.tabs", tabList().map(([id, label]) =>
      h(`button${view === id ? ".active" : ""}`, { onclick: () => go(`#/media/${id}`) }, label)));
    root.append(
      h("div.m-head", h("h1", t("Media")),
        h("div.m-search", icon("search"), search)),
      tabs, h("div#m-warn"), results);
    // search in place (no hash navigation = no page re-render under the keyboard); a slower
    // older answer never replaces a newer one; Enter searches at once and closes the keyboard
    let lastQ = view === "search" ? rest.join("/").trim() : "";
    const runSearch = async (q) => {
      q = q.trim();
      if (q === lastQ) return;
      lastQ = q;
      if (q.length < 2) {
        if (!q) { history.replaceState(null, "", "#/media"); await renderDiscover(results); }
        return;
      }
      history.replaceState(null, "", `#/media/search/${enc(q)}`);
      tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      await renderSearch(results, q, () => lastQ === q);
    };
    const doSearch = debounce(runSearch, 700);
    search.addEventListener("input", () => doSearch(search.value));
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); search.blur(); runSearch(search.value); }
    });

    P("status").then((s) => { status = s; warnings(root.querySelector("#m-warn"), s); }).catch(() => {});

    if (view === "search") await renderSearch(results, rest.join("/"));
    else if (view === "watchlist") await renderWatchlist(results, on);
    else if (view === "downloads") await renderDownloads(results, on);
    else if (view === "library") await renderLibrary(results);
    else await renderDiscover(results, rest[0], rest[1]);
    return cleanup;
  },
};

// ---------------------------------------------------------------- shared bits

function warnings(el, s) {
  // the "add a TMDB key" hint lives in Settings' suggestions, not on every visit here
  const out = [];
  if (s.transmission && s.transmission.enabled !== false && s.transmission.ok === false)
    out.push(h("div.m-banner", "⚠️", h("div", t("Downloads are unavailable at the moment."))));
  fill(el, ...out);
}

function poster(it, clickable = true) {
  clickable = clickable !== false;  // Array.map passes the index (0 on the first card!)
  const open = () => it.id && it.type ? go(itemHash(it)) : toast(t("This title can't be opened (no id)"), true);
  const img = it.poster ? h("img", { src: it.poster, loading: "lazy", alt: "" }) : h("div.ph", it.title);
  return h("div.poster", { onclick: clickable ? open : null, title: it.title,
                          style: clickable ? null : { cursor: "default" } },
    h("div.img", img,
      h("div.badges",
        it.in_library && h("span.badge.lib", { title: t("In your library") }, icon("check")),
        it.favourite && h("span.badge.fav", { title: t("To watch") }, icon("heart"))),
      it.rating && h("span.rating", `★ ${fmt.num(it.rating, 1)}`),
      it.type === "series" && h("span.kindtag", t("Series"))),
    h("div.t", it.title),
    it.year ? h("div.y", String(it.year)) : null);
}

// release rejection reasons come from hub/plugins/media/ranking.py (English codes)
function reason(r) {
  const m = /^(too big) \((.+)\)$/.exec(r) || /^(language): (.+)$/.exec(r) || /^(wrong) (.+)$/.exec(r);
  if (m && m[1] === "too big") return t("too big ({size})", { size: m[2] });
  if (m && m[1] === "language") return t("language: {langs}", { langs: m[2] });
  if (m && m[1] === "wrong") {
    const n = /^(season|episode) (\d+)$/.exec(m[2]);
    const what = n ? (n[1] === "season" ? t("season {n}", { n: n[2] }) : t("episode {n}", { n: n[2] }))
      : { "complete series": t("complete series"), "no season info": t("no season info"),
          "single episode": t("single episode") }[m[2]] || m[2];
    return t("wrong {what}", { what });
  }
  return { "different title": t("different title"), "cam/telesync": t("cam/telesync"), "no seeders": t("no seeders"),
           "4K not allowed": t("4K not allowed") }[r] || r;
}

const loading = () => h("div.empty", h("div.spin"));

// placeholder posters (same size as real ones) while a rail/grid loads
const withImage = (items) => items.filter((it) => it.poster);
const skeletons = (n = 4) => Array.from({ length: n }, () =>
  h("div.poster.skel", h("div.img"), h("div.t"), h("div.y")));
const skelRail = () => [h("div.section", h("h2.skel-h", "\u00a0")), h("div.m-row", skeletons(4))];

// friendly empty state: icon, one sentence, a button that leads somewhere
const emptyState = (ico, text, label, hash) =>
  h("div.empty.m-empty", h("div.m-empty-ico", icon(ico)), h("p", text),
    h("button.primary", { onclick: () => go(hash) }, label));

// ---------------------------------------------------------------- discover / search

async function renderDiscover(el, kind, genre) {
  kind = kind === "series" ? "series" : "movie";
  genre = genre ? decodeURIComponent(genre) : "";
  const chips = h("div.m-chips-in");
  const out = h("div");
  const seg = h("div.seg.m-kind");
  const strip = h("div.m-chips", seg, chips);
  let genres = [];
  let token = null;
  const setHash = () => history.replaceState(null, "", genre ? `#/media/discover/${kind}/${enc(genre)}` : "#/media");
  const pick = (id) => { genre = id === genre ? "" : id; paintChips(); show(); };
  const paintChips = () => {
    fill(seg, ...[["movie", t("Movies")], ["series", t("Series")]].map(([k, l]) =>
      h(`button${k === kind ? ".active" : ""}`, { onclick: () => { if (k !== kind) { kind = k; genre = ""; start(); } } }, l)));
    fill(chips, ...genres.map((g) =>
      h(`button.chip.g${g.id === genre ? ".on" : ""}`, { onclick: () => pick(g.id) }, genreName(g.name))));
    chips.querySelector(".on")?.scrollIntoView({ block: "nearest", inline: "center" });
  };
  async function show() {
    setHash();
    const mine = token = {};
    if (!genre) return rows(mine);
    const g = genres.find((x) => x.id === genre);
    const grid = h("div.m-grid");
    const more = h("button", t("More"));
    const box = h("div.m-more", more);
    let page = 0, busy = false;
    async function next() {
      if (busy) return;
      busy = true; more.disabled = true;
      try {
        const r = await P(`discover/genre?type=${kind}&genre=${enc(genre)}&page=${page + 1}`);
        if (mine !== token) return;
        page = r.page;
        grid.querySelectorAll(".skel").forEach((x) => x.remove());
        grid.append(...withImage(r.items).map((it) => poster(it)));
        if (!grid.children.length && !r.has_more) box.replaceChildren(h("div.empty", t("Nothing found.")));
        else if (!r.has_more) box.remove();
      } catch (e) { grid.querySelectorAll(".skel").forEach((x) => x.remove()); toast(t("Couldn't load genre: {error}", { error: e.message }), true); }
      finally { busy = false; more.disabled = false; }
    }
    more.onclick = next;
    fill(out, h("div.section", h("h2", genreName(g ? g.name : genre)), h("div.spacer"),
                h("button.ghost", { onclick: () => pick(genre) }, icon("x"), t("Clear"))),
         grid, box);
    grid.append(...skeletons(6));
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((e) => e[0].isIntersecting && page && mine === token && next(),
                               { rootMargin: "400px" }).observe(box);
    }
    await next();
  }
  async function rows(mine) {
    fill(out, skelRail(), skelRail());
    const lib = Promise.all(["movies", "series"].map((k) => P(`library?kind=${k}`).then((r) =>
      r.items.filter((x) => x.poster && x.meta_id).map((x) => ({ ...x, type: k === "movies" ? "movie" : "series", id: x.meta_id }))).catch(() => [])));
    let d;
    try { d = await P("discover"); } catch (e) { toast(e.message, true); d = { sections: [] }; }
    const mineLib = (await lib).flat().sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 20);
    if (mine !== token) return;
    const by = (...keys) => keys.map((k) => d.sections.find((s) => s.key === k)).find((s) => s && withImage(s.items).length);
    const secs = [
      mineLib.length && { title: t("In your library"), items: mineLib.map((x) => ({ ...x, in_library: false })) },
      by("movie_popular"), by("tv_popular"),
      by("now_playing", "new_movies", "on_the_air", "new_series", "upcoming")
        && { ...by("now_playing", "new_movies", "on_the_air", "new_series", "upcoming"), title: t("What's new") },
    ].filter(Boolean);
    fill(out, ...secs.map((s) => [
      h("div.section", h("h2", s.title)),
      h("div.m-row", withImage(s.items).map((it) => poster(it))),
    ]).flat());
    if (!secs.length) fill(out, h("div.empty", t("Couldn't reach the movie database.")));
  }
  async function start() {
    genres = [];
    paintChips();
    try { genres = (await P(`genres?type=${kind}`)).genres; } catch (e) { toast(t("Genres unavailable: {error}", { error: e.message }), true); }
    if (genre && !genres.some((g) => g.id === genre)) genre = "";
    paintChips();
    await show();
  }
  fill(el, strip, out);
  await start();
}

async function renderSearch(el, q, current = () => true) {
  // keep the previous results on screen (dimmed) while the new ones load: no jumping
  if (el.querySelector(".m-grid") && !el.querySelector(".skel")) el.classList.add("m-stale");
  else fill(el, h("div.m-grid", skeletons(6)));
  let items;
  try { items = await P(`search?q=${enc(q)}`); } finally { if (current()) el.classList.remove("m-stale"); }
  if (!current()) return;
  fill(el, 
    h("div.section", h("h2", t("Results for “{q}”", { q })), h("span.muted", items.length)),
    items.length ? h("div.m-grid", items.map((it) => poster(it))) : h("div.empty", t("Nothing found.")));
}

// ---------------------------------------------------------------- detail

async function renderDetail(root, type, id, on) {
  fill(root, loading());
  const [it] = await Promise.all([P(`details/${type}/${enc(id)}`), status ? null : P("status").then((s) => { status = s; }).catch(() => {})]);
  const body = h("div.detail-body");
  fill(root, 
    h("div.hero", { style: { backgroundImage: it.backdrop ? `url("${it.backdrop}")` : "" } },
      h("button.icon.back", { onclick: () => history.length > 1 ? history.back() : go("#/media") }, icon("left")),
      h("div.inner",
        it.poster ? h("img.pimg", { src: it.poster, alt: "" }) : h("div.pimg"),
        h("div.stack", { style: { gap: "6px", minWidth: 0 } },
          h("h1", it.title),
          h("div.row.wrap.small.muted",
            [it.year, typeof it.runtime === "number" ? t("{n} min", { n: it.runtime }) : it.runtime, it.type === "series" ? t("Series") : t("Movie"),
             ...(it.genres || []).slice(0, 3).map(genreName)].filter(Boolean).join(" · "),
            it.rating && h("span.chip", `★ ${fmt.num(it.rating, 1)}`))))),
    body);

  const favBtn = h("button.m-tow", { onclick: () => toggleFav() });
  const autoSw = h("input", { type: "checkbox" });
  const favRow = h("div.row.small");
  const paint = () => {
    const w = it.watch;
    fill(favBtn, icon("heart"), t("To watch"));
    favBtn.className = `m-tow${w ? " on" : ""}`;
    favBtn.setAttribute("aria-pressed", w ? "true" : "false");
    fill(favRow, ...(w ? [
      h("label.row", { style: { gap: "6px", minHeight: "44px" } }, autoSw, t("Download automatically when found")),
      w.note && h("span.chip.accent", w.note)] : [
      h("span.muted", t("I'll let you know when it's available in Italian."))]));
    autoSw.checked = !!w?.auto_download;
  };
  async function toggleFav() {
    await act(async () => {
      if (it.watch) { await P(`watchlist/${enc(`${type}:${it.id}`)}`, { method: "DELETE" }); it.watch = null; toast(t("Removed from your list")); }
      else { it.watch = await P("watchlist", { body: { type, id: it.id } }); toast(t("❤️ Added - I'll keep an eye on it")); }
      paint();
    }, favBtn);
  }
  autoSw.onchange = () => act(async () => {
    it.watch = await P(`watchlist/${enc(`${type}:${it.id}`)}`, { method: "PATCH", body: { auto_download: autoSw.checked } });
    paint();
  });
  paint();

  const stateLine = h("div.m-state");
  const getBtn = type === "movie" && !it.in_library ? h("button.primary", { onclick: (e) => quickDownload(it, {}, e.currentTarget) },
    icon("download"), t("Download this")) : null;
  const actions = h("div.row.wrap.m-actions", getBtn,
    type === "movie" && h("button.ghost.small", { onclick: () => openReleases(it, {}) }, t("Choose version")),
    h("div.spacer"),
    it.trailer && h("a.btn.icon", { href: it.trailer, target: "_blank", rel: "noopener", title: t("Trailer"), "aria-label": t("Trailer") }, icon("play")),
    favBtn);

  const dlBox = h("div");
  const paintDl = (items) => {
    const mine = items.filter((d) => d.key === `${type}:${it.id}` && !["imported", "removed"].includes(d.state));
    const pct = mine.length ? Math.round(mine.reduce((a, d) => a + d.progress, 0) / mine.length * 100) : null;
    fill(stateLine,
      it.in_library ? h("div.row.wrap", h("span.chip.ok", icon("check"), t("In your library")), infuseLink())
        : pct != null ? h("div.row.wrap", h("span.chip.accent", t("Downloading · {pct}%", { pct }))) : null);
    if (getBtn) getBtn.hidden = pct != null;
    fill(dlBox, ...(type === "series" && mine.length ? [h("div.list", mine.map(dlRow))] : []));
  };
  paintDl(it.downloads || []);
  on((e) => e.detail.kind === "downloads" && paintDl(e.detail.items));

  const english = it.overview && (status?.metadata !== "tmdb" || looksEnglish(it.overview));
  fill(body, stateLine, actions,
    type === "movie" && it.in_library && it.library_path && subsButton(it),
    favRow, dlBox,
    it.overview && h("div.stack", { style: { gap: "4px" } }, h("p.overview", { style: { margin: 0 } }, it.overview),
      english && h("span.small.muted", t("Plot in English"))),
    it.cast?.length && h("div.small.muted", t("With "), it.cast.slice(0, 6).join(", ")));

  if (hasPlugin("sync")) sharedBlock(it, type).then((el) => el && favRow.after(el)).catch(() => {});
  if (type === "series") body.append(await seasonsView(it, on));
}

// Subtitles in the first audio language: search/check/re-sync on demand, state shown as a chip.
function subsButton(it) {
  const lang = status?.profile?.languages?.[0] || "it";
  const name = { it: t("Italian"), en: t("English"), es: t("Spanish"), fr: t("French"), de: t("German"), pt: t("Portuguese") }[lang] || lang.toUpperCase();
  const chip = h("span.chip");
  const btn = h("button", { onclick: () => act(async () => {
    await P("subtitles", { body: { path: it.library_path, title: it.title, year: it.year, imdb_id: it.imdb_id } });
    toast(t("Looking for {lang} subtitles…", { lang: name }));
    await poll();
  }, btn) }, t("{lang} subtitles", { lang: name }));
  const LABEL = { found: t("ready"), in_sync: t("ready"), adapted: t("found · timing adjusted"), unverified: t("found · unverified"),
                  not_found: t("not found"), error: t("error"), skipped: t("not needed"), searching: t("searching…") };
  async function poll(n = 0) {
    const r = await P(`subtitles?path=${enc(it.library_path)}`);
    const st = r.states.map((s) => s.status).filter((s) => s !== "none");
    const key = st.includes("searching") ? "searching" : ["error", "not_found", "unverified", "adapted", "in_sync", "found", "skipped"].find((k) => st.includes(k));
    chip.textContent = key ? `${LABEL[key]}${st.length > 1 ? ` (${st.length})` : ""}` : "";
    chip.className = `chip ${key === "searching" ? "accent" : ["not_found", "error", "unverified"].includes(key) ? "warn" : key ? "ok" : ""}`;
    chip.title = r.states.map((s) => s.message || s.release || "").filter(Boolean).join("\n");
    if (key === "searching" && n < 120) setTimeout(() => poll(n + 1).catch(() => {}), 3000);
  }
  poll().catch(() => {});
  return h("span.row", { style: { gap: "6px" } }, btn, chip);
}

// "Shared feed" (hub/plugins/sync): my watched/rating state + what friends said. Hidden if the plugin errors.
async function sharedBlock(it, type) {
  ensureCss(`/static/pages/social.css?v=${window.CASA_V}`);
  const S = (path, opts) => api(`p/sync/${path}`, opts);
  const val = (x, k) => (x && typeof x === "object" ? x[k] : x);
  let d = await S(`lookup?type=${type}&id=${enc(it.id)}`);
  const box = h("div.s-share");
  const post = (ev) => act(async () => {
    await S("events", { body: { type, id: it.id, ...ev } });
    d = await S(`lookup?type=${type}&id=${enc(it.id)}`);
    paint();
  });
  const today = () => { const n = new Date(); return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, "0")}-${String(n.getDate()).padStart(2, "0")}`; };
  const paint = () => {
    const watched = val(d.mine?.watch, "watched_at");
    const rating = val(d.mine?.rating, "rating");
    const note = h("input", { placeholder: t("Note (optional)"), maxlength: 500, value: val(d.mine?.rating, "note") || "" });
    const rate = h("div.s-rate", [1, 2, 3, 4, 5].map((n) => {
      const cls = rating >= n ? ".on" : rating >= n - 0.5 ? ".half" : "";
      return h(`button${cls}`, { title: `${n}`, "aria-label": t("{n} stars", { n }), onclick: (e) => {
        const r = e.offsetX < e.currentTarget.offsetWidth / 2 ? n - 0.5 : n;
        post({ kind: "rating", rating: r === rating ? null : r, note: note.value });
      } }, "★");
    }));
    const others = (d.others || []).map((o) => {
      const bits = [val(o.watch, "watched_at") && t("watched"), val(o.rating, "rating") != null && `★ ${val(o.rating, "rating")}`].filter(Boolean);
      const n = val(o.rating, "note") || o.note;
      return h("div", h("b", o.user), bits.length ? ` ${bits.join(" · ")}` : "", n && h("span.muted", ` “${n}”`));
    });
    fill(box, h("h3", t("Shared feed")),
      h("div.row.wrap", { style: { gap: "10px" } },
        watched
          ? [h("span.chip.ok", icon("check"), t("Watched {date}", { date: fmt.date(watched, { weekday: false }) })),
             h("button", { onclick: () => post({ kind: "watch", watched_at: null }) }, t("Undo"))]
          : h("button", { onclick: () => post({ kind: "watch", watched_at: today() }) }, icon("check"), t("Mark watched")),
        rate),
      h("div.row", { style: { gap: "8px" } }, note,
        rating != null && h("button", { onclick: () => post({ kind: "rating", rating, note: note.value }) }, t("Save note"))),
      others.length && h("div.s-others", others));
  };
  paint();
  return box;
}

async function seasonsView(it, on) {
  const seasons = it.seasons || [];
  if (!seasons.length) return h("div.empty", t("No season information."));
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
        h("b", t("Season {n}", { n: current })), h("span.muted.small", t("{have}/{total} in library", { have: eps.filter((e) => e.in_library).length, total: eps.length })),
        h("div.spacer"),
        missing.length > 1 && h("button.primary", { onclick: (e) => quickDownload(it, { season: current }, e.currentTarget) },
          icon("download"), t("Download season")),
        h("button", { onclick: () => openReleases(it, { season: current }) }, t("Season packs"))),
      h("div.list", eps.map((e) => h("div.item.ep",
        h("div.num", e.episode),
        e.still ? h("img.still", { src: e.still, loading: "lazy", alt: "" }) : null,
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("b.ellipsis", e.title || t("Episode {n}", { n: e.episode })),
          h("div.small.muted", e.air_date ? fmt.date(e.air_date, { weekday: false }) : t("TBA"))),
        e.in_library ? h("span.chip.ok", icon("check"), t("Library"))
          : e.downloading ? h("span.chip.accent", `${Math.round(e.downloading.progress * 100)}%`)
          : !e.aired ? h("span.chip", t("Not aired"))
          : [h("button.icon", { title: t("Download best"), onclick: (ev) => quickDownload(it, { season: current, episode: e.episode }, ev.currentTarget) }, icon("download")),
             h("button.icon.ghost", { title: t("Choose release"), onclick: () => openReleases(it, { season: current, episode: e.episode }) }, icon("more"))]))));
  }
  const reload = debounce(load, 1500);
  on((e) => e.detail.kind === "downloads" && e.detail.items.some((d) => d.state === "imported") && reload());
  paintSeg();
  wrap.append(h("div.section", h("h2", t("Episodes"))), seg, list);
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
              : where.season ? t("Season {n}", { n: where.season }) : it.year;
  const s = sheet(`${it.title} · ${label}`, loading(), { wide: true });
  const qs = new URLSearchParams(Object.entries(where).filter(([, v]) => v != null));
  try {
    const r = await P(`releases/${it.type}/${enc(it.id)}?${qs}`);
    const ok = r.releases.filter((x) => !x.rejected);
    const bad = r.releases.filter((x) => x.rejected);
    const sources = Object.entries(r.stats).map(([k, v]) =>
      h(`span.chip${v.error ? ".bad" : ""}`, { title: v.error || "" }, `${k} ${v.error ? "✕" : v.count}`));
    const showBad = h("button.ghost.small", bad.length ? t("Show {n} rejected", { n: bad.length }) : "");
    const badList = h("div.list.hidden", bad.slice(0, 60).map((x) => relRow(it, where, x, false, s)));
    showBad.onclick = () => { badList.classList.toggle("hidden"); };
    fill(s.el, 
      h("div.row.wrap", ...sources),
      ok.length ? h("div.list", ok.map((x, i) => relRow(it, where, x, i === 0, s)))
                : h("div.empty", t("No acceptable release yet. Add it to favourites and you'll be notified.")),
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
        best && h("span.chip.accent", t("★ Best match")),
        x.quality && h("span.chip", x.quality),
        h("span.chip", fmt.bytes(x.size)),
        h(`span.chip${x.seeders > 20 ? ".ok" : x.seeders < 3 ? ".warn" : ""}`, `▲ ${x.seeders}`),
        h("span.small.muted", x.source),
        x.subs?.includes("it") && x.language !== "it" && h("span.chip", t("sub ITA")),
        x.rejected && h("span.chip.bad", reason(x.rejected)))),
    !x.rejected || x.rejected.startsWith("language") || x.rejected.startsWith("too big") ?
      h(`button.icon${best ? ".primary" : ""}`, { title: t("Download"), onclick: (e) => act(async () => {
        await P("download", { body: { type: it.type, id: it.id, hash: x.hash, ...where } });
        toast(t("⬇ Sent to downloads"));
        s.close();
      }, e.currentTarget) }, icon("download")) : null);
}

// ---------------------------------------------------------------- favourites

const WLABEL = () => ({ in_library: t("In your library"), downloading: t("Downloading"), found: t("Available"),
                       up_to_date: t("Up to date"), waiting: t("Waiting"), upcoming: t("Upcoming"), new: t("New"), error: t("Error") });
const wlabel = (st) => WLABEL()[st] || st.replace(/_/g, " ");
const WSTATUS = { in_library: "ok", downloading: "accent", found: "accent", up_to_date: "ok", waiting: "", upcoming: "warn" };

async function renderWatchlist(el, on) {
  const paint = async () => {
    const items = await P("watchlist");
    if (!items.length) {
      fill(el, emptyState("heart", t("Tap the heart on a film or series and I'll tell you when it's ready to watch."),
                          t("Discover movies"), "#/media"));
      return;
    }
    fill(el, h("div.section", h("h2", t("To watch")), h("span.muted", items.length)),
      h("div.list", items.map((w) => h("div.item",
        w.poster ? h("img", { src: w.poster, style: { width: "46px", height: "69px", borderRadius: "6px", objectFit: "cover" } }) : null,
        h("div.stack", { style: { gap: "4px", flex: 1, minWidth: 0, cursor: "pointer" }, onclick: () => go(`#/media/${w.type}/${enc(w.id)}`) },
          h("b.ellipsis", w.title, w.year ? h("span.muted", ` (${w.year})`) : ""),
          h("div.row.wrap", h(`span.chip${WSTATUS[w.status] ? "." + WSTATUS[w.status] : ""}`, wlabel(w.status || "new")),
            h("span.small.muted", w.note || ""),
            w.auto_download && h("span.chip", icon("download"), t("auto")))),
        h("button.icon.ghost", { title: t("Check now"), onclick: (e) => act(async () => {
          await P(`watchlist/${enc(w.key)}/check`, { body: {} }); paint(); }, e.currentTarget) }, icon("refresh")),
        h("button.icon.ghost", { title: t("Remove"), onclick: (e) => act(async () => {
          await P(`watchlist/${enc(w.key)}`, { method: "DELETE" }); paint(); }, e.currentTarget) }, icon("trash"))))));
  };
  fill(el, loading());
  await paint();
  on((e) => e.detail.kind === "watchlist" && paint());
}

// ---------------------------------------------------------------- downloads

const DLABEL = () => ({ downloading: t("Downloading"), queued: t("Queued"), paused: t("Paused"), completed: t("Completed"),
                       seeding: t("Seeding"), imported: t("Imported"), failed: t("Failed"), removed: t("Removed") });
const dlabel = (st) => DLABEL()[st] || st;
const DSTATE = { downloading: "accent", queued: "", paused: "warn", completed: "accent", seeding: "ok",
                 imported: "ok", failed: "bad", removed: "" };

function dlRow(d) {
  const active = ["queued", "downloading", "paused"].includes(d.state);
  const what = d.season != null ? ` S${String(d.season).padStart(2, "0")}${d.episode != null ? "E" + String(d.episode).padStart(2, "0") : ""}` : "";
  return h("div.item.dl",
    d.data?.poster ? h("img", { src: d.data.poster, alt: "" }) : h("div.tile", h("div.ico", icon("film"))),
    h("div", { style: { flex: 1, minWidth: 0 } },
      h("div.row", h("b.ellipsis", d.title + what),
        h(`span.chip${DSTATE[d.state] ? "." + DSTATE[d.state] : ""}`, d.state === "imported" ? t("Ready to watch") : dlabel(d.state))),
      d.state === "imported" ? h("div.row", { style: { marginTop: "4px" } }, infuseLink()) : null,
      active && h("div.progress", { style: { marginTop: "8px" } }, h("div", { style: { width: `${(d.progress * 100).toFixed(1)}%` } })),
      h("div.row2",
        active ? h("span", `${Math.round(d.progress * 100)}%`) : null,
        active && d.eta > 0 ? h("span", t("ETA {time}", { time: fmt.duration(d.eta) })) : null,
        d.error ? h("span", { style: { color: "var(--bad)" } }, d.error) : null)),
    d.state === "downloading" && h("button.icon.ghost", { title: t("Pause"), onclick: (e) => dlAct(d, "pause", e) }, icon("pause")),
    d.state === "paused" && h("button.icon.ghost", { title: t("Resume"), onclick: (e) => dlAct(d, "resume", e) }, icon("play")),
    d.state === "failed" && h("button.icon.ghost", { title: t("Retry import"), onclick: (e) => dlAct(d, "retry_import", e) }, icon("refresh")),
    h("button.icon.ghost", { title: active ? t("Cancel & delete partial data") : t("Remove from list"), onclick: (e) => {
      if (!active || confirm(t("Cancel {title}?", { title: d.title }))) dlAct(d, "remove", e);
    } }, icon("x")));
}

const dlAct = (d, a, e) => act(() => P(`downloads/${d.hash}/${a}`, { body: {} }), e.currentTarget);

async function renderDownloads(el, on) {
  const paint = (items) => {
    const active = items.filter((d) => ["queued", "downloading", "paused", "completed", "seeding"].includes(d.state));
    const done = items.filter((d) => !active.includes(d));
    fill(el, 
      h("div.section", h("h2", t("Downloading")), h("span.muted", active.length)),
      active.length ? h("div.list", active.map(dlRow))
        : emptyState("download", t("Nothing is downloading right now."), t("Discover movies"), "#/media"),
      done.length ? [h("div.section", h("h2", t("Finished"))), h("div.list", done.slice(0, 30).map(dlRow))] : null);
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
  const stats = h("div.m-libstat");
  const scanBtn = h("button", { onclick: () => act(async () => { await P("library/scan", { body: {} }); toast(t("Library rescanned")); load(); }, scanBtn) },
    icon("refresh"), t("Rescan"));
  const seg = h("div.seg");
  const paintSeg = () => fill(seg, ...[["movies", t("Movies")], ["series", t("Series")]].map(([k, l]) =>
    h(`button${k === kind ? ".active" : ""}`, { onclick: () => { kind = k; paintSeg(); load(); } }, l)));
  async function load() {
    const r = await P(`library?kind=${kind}`);
    const s = r.stats;
    stats.textContent = `${t("{n} films", { n: s.movies })} · ${t("{n} series", { n: s.shows })}`;
    const missing = [!s.movies_dir_ok && t("movies folder not found: {dir}", { dir: s.movies_dir || t("(not set)") }),
                     !s.series_dir_ok && t("series folder not found: {dir}", { dir: s.series_dir || t("(not set)") })].filter(Boolean);
    fill(grid, 
      missing.length ? h("div.m-banner", "📁", h("div", missing.join(" · "), t(" - is the NAS mounted?"))) : null,
      r.items.length ? h("div.m-grid", r.items.map((x) => poster({
        ...x, type: kind === "movies" ? "movie" : "series", id: x.meta_id,
        in_library: false, title: x.title, year: x.year,
      }, !!x.meta_id))) : emptyState("film", t("Your library is empty. Films you download will show up here."),
                                                                       t("Discover movies"), "#/media"));
  }
  const tech = await sourcesPanel();
  const down = status?.transmission && status.transmission.enabled !== false && status.transmission.ok === false;
  const noSources = tech.info.enabled.length && tech.info.enabled.every((n) => tech.info.health?.[n]?.ok === false);
  fill(el, 
    h("div.section", h("h2", t("Your library"))), stats,
    (noSources && !down) ? h("div.m-banner", "⚠️", h("div", t("Downloads are unavailable at the moment."))) : null,
    h("div.section", seg), grid,
    h("details.m-tech", h("summary", t("Technical details")),
      h("div.stack", { style: { marginTop: "10px" } }, h("div.row", scanBtn), tech.box)));
  paintSeg();
  await load();
}

async function sourcesPanel() {
  const box = h("div.stack");
  const out = { box, info: await P("indexers") };
  const paint = (info) => {
    out.info = info;
    const hl = info.health || {};
    fill(box, 
      h("div.list",
        h("div.item", icon("link"), h("div", { style: { flex: 1 } }, h("b", t("Proxy")),
          h("div.small.muted", info.proxy || t("none - set [plugins.media.proxy] url = \"socks5h://…\" to reach blocked trackers"))),
          info.error && h("span.chip.bad", info.error)),
        info.enabled.map((n) => {
          const x = hl[n] || {};
          return h("div.item",
            h(`span.chip${x.ok ? ".ok" : x.ok === false ? ".bad" : ""}`, x.ok ? t("online") : x.ok === false ? t("offline") : "…"),
            h("div", { style: { flex: 1, minWidth: 0 } }, h("b", n),
              h("div.small.muted.ellipsis", x.ok ? `${x.mirror} · ${x.latency_ms} ms` : x.error || t("not checked"))));
        })),
      h("div.row", h("span.small.muted", status?.transmission ? t("Transmission: {state}", { state: status.transmission.ok ? t("connected") : status.transmission.error || "?" }) : ""),
        h("div.spacer"), testBtn));
  };
  const testBtn = h("button", { onclick: () => act(async () => paint(await P("indexers/test", { body: {} })), testBtn) },
    icon("refresh"), t("Test sources"));
  paint(out.info);
  return out;
}
