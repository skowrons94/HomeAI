// Music: your Navidrome library, discovery on MusicBrainz, tracker downloads and playback on the house
// speakers (hub/plugins/music/). Route: #/music.
// Tabs: Library | Discover | Downloads. A mini-player bar sits above the app tab bar. Playback goes either to
// this phone (<audio> on the proxied stream) or to an AirPlay output (the hub streams and keeps the queue).
import { api, h, icon, toast, ensureCss, act, fmt, t, sheet, debounce } from "../app.js";

const P = (path, opts) => api(`p/music/${path}`, opts);
const KEY = "casa.music.view";
const PHONE = "phone";
const LISTS = () => [["newest", t("New")], ["recent", t("Recent")], ["frequent", t("Most played")],
  ["random", t("Random")], ["alphabeticalByName", t("A–Z")], ["artists", t("Artists")]];

let view = { tab: "library", list: "newest", dtab: "artist", output: PHONE };
try { view = { ...view, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* private mode */ }
const saveView = () => { try { localStorage.setItem(KEY, JSON.stringify(view)); } catch { /* ignore */ } };

let root = null;
let timers = [];
const els = {};
let lib = { albums: [], artists: [], configured: true, error: "", loaded: false, search: null };
let query = "";
const dx = { q: "", artists: [], albums: [], busy: false, searched: false, error: "" };
let downloads = [];
const seen = new Map();
let outputs = { list: [], default: "", loaded: false };

// ---- small helpers ------------------------------------------------------------------------

const svg = (vb, inner, cls) => { const s = h("svg", { viewBox: vb, class: cls }); s.innerHTML = inner; return s; };
const LOCAL = {
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  shuffle: '<path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
};
const ic = (name) => (LOCAL[name] ? svg("0 0 24 24", LOCAL[name], "i") : icon(name));

const hue = (s) => { let n = 0; for (const c of String(s)) n = (n * 31 + c.charCodeAt(0)) % 360; return n; };
const initials = (s) => {
  const w = String(s || "?").replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2)).toUpperCase();
};

/** Square cover: a coloured placeholder with initials, the image fades in over it (and is dropped if it 404s). */
function cover(src, title, cls = "") {
  const box = h(`div.mu-cov${cls}`, { style: `--mh:${hue(title)}` }, h("span", initials(title)));
  if (src) box.append(h("img", { src, loading: "lazy", alt: "", onerror: (e) => e.target.remove() }));
  return box;
}
const navCover = (id, title, size = 300, cls = "") => cover(id ? `/api/p/music/cover/${encodeURIComponent(id)}?size=${size}` : "", title, cls);
const mbCover = (mbid, title, cls = "") => cover(mbid ? `/api/p/music/mbcover/${mbid}` : "", title, cls);
const mins = (s) => (s ? fmt.duration(s) : "");
const total = (tracks) => tracks.reduce((a, x) => a + (x.duration || 0), 0);
const joinMeta = (...p) => p.filter((x) => x || x === 0).join(" · ");

function chip(label, count, on, fn) {
  return h("button.chip.mu-fchip" + (on ? ".on" : ""), { onclick: fn, "aria-pressed": on },
    label, count != null ? h("span.mu-count", fmt.num(count)) : null);
}

const put = (el, ...kids) => el.replaceChildren(...kids.flat().filter((x) => x != null && x !== false));
const section = (title, count) => h("h3.mu-section", title, count != null ? h("span.mu-count", fmt.num(count)) : null);

// ---- player (this phone, or an AirPlay output driven by the hub) ---------------------------------

const audio = new Audio();
audio.preload = "auto";
const pl = { queue: [], index: -1, state: "idle", error: "", remote: null, remoteAt: 0, volume: 100, listeners: new Set() };
const emit = () => pl.listeners.forEach((f) => f());
const isPhone = () => view.output === PHONE;
const outName = () => (isPhone() ? t("This phone") : (outputs.list.find((o) => o.id === view.output)?.name || t("Speaker")));

function now() {
  if (isPhone()) {
    const tr = pl.queue[pl.index] || null;
    return { state: tr ? pl.state : "idle", track: tr, position: audio.currentTime || 0,
      duration: (Number.isFinite(audio.duration) && audio.duration) || tr?.duration || 0, volume: Math.round(audio.volume * 100),
      error: pl.error, index: pl.index, queue: pl.queue };
  }
  const r = pl.remote || {};
  const live = r.state === "playing" ? (Date.now() - pl.remoteAt) / 1000 : 0;
  return { state: r.state || "idle", track: r.track || null, position: Math.min((r.position || 0) + live, r.duration || 1e9),
    duration: r.duration || r.track?.duration || 0, volume: r.volume ?? 50, error: r.error || "", index: r.index ?? -1, queue: r.queue || [] };
}

const streamUrl = (tr) => `/api/p/music/stream/${encodeURIComponent(tr.id)}?suffix=${encodeURIComponent(tr.suffix || "")}`;

function phoneLoad(i) {
  const tr = pl.queue[i];
  if (!tr) { pl.state = "idle"; pl.index = -1; audio.removeAttribute("src"); emit(); return; }
  pl.index = i; pl.error = "";
  audio.src = streamUrl(tr);
  audio.play().catch((e) => { pl.error = e.message; toast(e.message, true); });
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: tr.title, artist: tr.artist, album: tr.album,
        artwork: tr.cover ? [{ src: `/api/p/music/cover/${encodeURIComponent(tr.cover)}?size=300`, sizes: "300x300" }] : [] });
    } catch { /* unsupported */ }
  }
  emit();
}
audio.addEventListener("play", () => { pl.state = "playing"; emit(); });
audio.addEventListener("pause", () => { if (audio.src && !audio.ended) { pl.state = "paused"; emit(); } });
audio.addEventListener("ended", () => { if (pl.index + 1 < pl.queue.length) phoneLoad(pl.index + 1); else { pl.state = "idle"; emit(); } });
audio.addEventListener("error", () => { if (audio.src) { pl.error = t("This track cannot be played on the phone"); pl.state = "paused"; emit(); } });
if ("mediaSession" in navigator) {
  const set = (a, f) => { try { navigator.mediaSession.setActionHandler(a, f); } catch { /* ignore */ } };
  set("play", () => control("resume")); set("pause", () => control("pause"));
  set("nexttrack", () => control("next")); set("previoustrack", () => control("previous"));
}

async function remote(path, body) {
  const r = await P(path, { body: { output: view.output, ...body } });
  pl.remote = r; pl.remoteAt = Date.now(); emit();
  return r;
}

/** tracks: full track objects (phone); album: id when they are all of that album (hub resolves it). */
async function playTracks(tracks, start = 0, { shuffle = false, album = null, mode = "play" } = {}) {
  if (!tracks.length) return;
  if (isPhone()) {
    let list = tracks.slice();
    if (mode === "queue" || mode === "next") {
      if (pl.state === "idle" || !pl.queue.length) mode = "play";
      else { pl.queue.splice(mode === "next" ? pl.index + 1 : pl.queue.length, 0, ...list); emit(); toast(t("Added to the queue")); return; }
    }
    if (shuffle) {
      const first = start ? list[start] : null;
      if (first) list.splice(start, 1);
      for (let i = list.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [list[i], list[j]] = [list[j], list[i]]; }
      if (first) list.unshift(first);
      start = 0;
    }
    pl.queue = list; phoneLoad(start);
    return;
  }
  const body = album ? { album, start, shuffle, mode } : { songs: tracks.map((x) => x.id), start, shuffle, mode };
  await remote("player/play", body);
  if (mode !== "play") toast(t("Added to the queue"));
}

async function control(action, extra = {}) {
  try {
    if (isPhone()) {
      if (action === "pause") audio.pause();
      else if (action === "resume") { if (audio.src) await audio.play(); }
      else if (action === "toggle") { if (audio.paused) await audio.play(); else audio.pause(); }
      else if (action === "next") { if (pl.index + 1 < pl.queue.length) phoneLoad(pl.index + 1); else control("stop"); }
      else if (action === "previous") { if (audio.currentTime > 3 || pl.index <= 0) audio.currentTime = 0; else phoneLoad(pl.index - 1); }
      else if (action === "stop") { audio.pause(); audio.removeAttribute("src"); pl.queue = []; pl.index = -1; pl.state = "idle"; emit(); }
      else if (action === "volume") audio.volume = Math.max(0, Math.min(1, extra.value / 100));
      else if (action === "jump") phoneLoad(extra.index);
      emit();
    } else await remote(`player/${action}`, extra);
  } catch (e) { toast(e.message, true); }
}
const sendVolume = debounce((v) => control("volume", { value: v }), 250);

async function pollRemote() {
  if (isPhone() || !root?.isConnected) return;
  try {
    const r = await P(`player?output=${encodeURIComponent(view.output)}`);
    pl.remote = r; pl.remoteAt = Date.now(); emit();
  } catch { /* output gone or hub busy */ }
}

// ---- mini-player bar -----------------------------------------------------------------------

let bar = null;
const barSig = () => { const n = now(); return JSON.stringify([n.state, n.track?.id, view.output, n.error, outputs.list.length]); };
let lastBarSig = "";
let volOpen = false;

function drawBar() {
  if (!bar) return;
  const n = now();
  const show = !!n.track || !!n.error;
  bar.hidden = !show;
  root?.classList.toggle("has-bar", show);
  if (!show) return;
  lastBarSig = barSig();
  const playing = n.state === "playing";
  const vol = h("input.mu-vol", { type: "range", min: 0, max: 100, step: 1, value: n.volume, "aria-label": t("Volume"),
    oninput: (e) => { if (isPhone()) control("volume", { value: +e.target.value }); else sendVolume(+e.target.value); } });
  put(bar, 
    h("div.mu-prog", h("div", { style: { width: "0%" } })),
    h("div.mu-bar-main",
      h("button.mu-bar-info", { onclick: () => openOutputs(), "aria-label": t("Choose where to play") },
        navCover(n.track?.cover, n.track?.title || "♪", 100, ".sm"),
        h("span.mu-bar-text",
          h("b", n.track?.title || t("Nothing playing")),
          h("span.mu-bar-sub", n.error ? n.error : joinMeta(n.track?.artist), ),
          h("span.mu-out", icon(isPhone() ? "volume" : "speaker"), h("span", outName())))),
      h("div.mu-bar-ctl",
        h("button.icon.ghost", { "aria-label": t("Previous"), onclick: () => control("previous") }, icon("prev")),
        h("button.icon.primary.mu-play", { "aria-label": playing ? t("Pause") : t("Play"), onclick: () => control("toggle") },
          icon(playing ? "pause" : "play")),
        h("button.icon.ghost", { "aria-label": t("Next"), onclick: () => control("next") }, icon("next")),
        h("button.icon.ghost", { "aria-label": t("Volume"), "aria-pressed": volOpen,
          onclick: () => { volOpen = !volOpen; drawBar(); } }, icon("volume")))),
    volOpen ? h("div.mu-bar-vol", icon("mute"), vol, icon("volume")) : null);
  tickBar();
}

function tickBar() {
  if (!bar || bar.hidden) return;
  const n = now();
  const f = bar.querySelector(".mu-prog > div");
  if (f) f.style.width = `${n.duration ? Math.min(100, (n.position / n.duration) * 100) : 0}%`;
}

// ---- data -----------------------------------------------------------------------------------

async function loadLibrary() {
  try {
    if (view.list === "artists") {
      lib.artists = (await P("artists")).artists || [];
      lib.configured = true;
    } else {
      const d = await P(`albums?type=${encodeURIComponent(view.list)}&size=${view.list === "alphabeticalByName" ? 100 : 36}`);
      lib.albums = d.albums || []; lib.configured = d.configured !== false;
    }
    lib.error = "";
  } catch (e) { lib.error = e.message; }
  lib.loaded = true;
  drawLibrary();
}

async function loadOutputs(scan = false) {
  try {
    const d = scan ? await P("outputs/scan", { body: {} }) : await P("outputs");
    outputs = { list: d.outputs || [], default: d.default || "", loaded: true };
  } catch { outputs.loaded = true; }
  // first visit: the default output from Settings, when it exists
  if (!localStorage.getItem(KEY) && outputs.default) {
    const o = outputs.list.find((x) => x.id === outputs.default || x.name.toLowerCase() === outputs.default.toLowerCase());
    if (o) { view.output = o.id; saveView(); }
  }
  if (view.output !== PHONE && outputs.loaded && outputs.list.length && !outputs.list.some((o) => o.id === view.output)) {
    view.output = PHONE; saveView();
  }
  drawBar();
}

async function loadDownloads() {
  try { downloads = await P("downloads"); if (!Array.isArray(downloads)) downloads = []; } catch { downloads = []; }
  let finished = false;
  for (const d of downloads) {
    if (seen.get(d.hash) && seen.get(d.hash) !== "done" && d.state === "done") finished = true;
    seen.set(d.hash, d.state);
  }
  drawDownloads(); drawTabs();
  if (finished) { toast(t("A download was added to your library")); loadLibrary(); }
}
const activeDl = () => downloads.filter((d) => d.state === "downloading" || d.state === "importing");

// ---- library tab ------------------------------------------------------------------------

function albumTile(a) {
  return h("button.mu-tile", { onclick: () => openAlbum(a.id), "aria-label": a.name },
    navCover(a.cover, a.name, 300, ".sq"),
    h("div.mu-title", a.name),
    h("div.mu-meta", joinMeta(a.artist, a.year)));
}

function artistRow(a) {
  return h("button.mu-row", { onclick: () => openArtist(a.id, a.name) },
    navCover(a.cover, a.name, 100, ".sm.round"),
    h("span.mu-row-t", h("b", a.name), h("span.mu-meta", t("{n} albums", { n: a.albums }))), icon("right"));
}

function songRow(s) {
  return h("button.mu-row", { onclick: () => playTracks([s], 0) },
    navCover(s.cover, s.album, 100, ".sm"),
    h("span.mu-row-t", h("b", s.title), h("span.mu-meta", joinMeta(s.artist, s.album))),
    h("span.mu-meta", mins(s.duration)));
}

function emptyCard() {
  if (!lib.loaded) return h("div.empty", h("span.spin"));
  if (!lib.configured || (lib.error && !lib.albums.length && !lib.artists.length)) {
    return h("div.mu-card.mu-center", h("div.mu-ico", ic("music")),
      h("b", lib.configured ? t("The library is not reachable") : t("Connect your library")),
      h("p.muted", lib.error || t("Music uses Navidrome. Enter its address, user and password in the settings.")),
      h("button.primary.lg", { onclick: openSettings }, icon("settings"), t("Music settings")));
  }
  return h("div.mu-card.mu-center", h("div.mu-ico", ic("music")), h("b", t("No music yet")),
    h("p.muted", t("Find an album in Discover and it lands here once downloaded.")),
    h("button.primary.lg", { onclick: () => setTab("discover") }, icon("search"), t("Discover")));
}

function drawLibrary() {
  if (!root?.isConnected) return;
  put(els.chips, ...LISTS().map(([k, l]) => chip(l, null, !query && view.list === k, () => {
    view.list = k; query = ""; els.q.value = ""; lib.search = null; saveView(); lib.loaded = false; drawLibrary(); loadLibrary();
  })));
  if (query.trim().length >= 2 && lib.search) {
    const s = lib.search;
    const out = [];
    if (s.artists.length) out.push(section(t("Artists"), s.artists.length), h("div.mu-card.mu-list", s.artists.map(artistRow)));
    if (s.albums.length) out.push(section(t("Albums"), s.albums.length), h("div.mu-grid", s.albums.map(albumTile)));
    if (s.songs.length) out.push(section(t("Songs"), s.songs.length), h("div.mu-card.mu-list", s.songs.map(songRow)));
    put(els.body, ...(out.length ? out : [h("div.empty", t("No results."))]));
    return;
  }
  if (view.list === "artists") {
    put(els.body, lib.artists.length ? h("div.mu-card.mu-list", lib.artists.map(artistRow)) : emptyCard());
  } else put(els.body, lib.albums.length ? h("div.mu-grid", lib.albums.map(albumTile)) : emptyCard());
}

const doSearch = debounce(async () => {
  const q = query.trim();
  if (q.length < 2) { lib.search = null; drawLibrary(); return; }
  put(els.body, h("div.empty", h("span.spin")));
  try { lib.search = await P(`search?q=${encodeURIComponent(q)}`); } catch (e) { lib.search = null; toast(e.message, true); }
  drawLibrary();
}, 350);

// ---- album + artist sheets ------------------------------------------------------------------

async function openAlbum(id) {
  const s = sheet("", [h("div.empty", h("span.spin"))]);
  s.el.parentElement.classList.add("mu-sheet");
  let a;
  try { a = await P(`album/${encodeURIComponent(id)}`); } catch (e) { s.close(); toast(e.message, true); return; }
  const tracks = a.tracks;
  const multiDisc = new Set(tracks.map((x) => x.disc)).size > 1;
  let lastDisc = 0;
  const rows = [];
  tracks.forEach((x, i) => {
    if (multiDisc && x.disc !== lastDisc) { lastDisc = x.disc; rows.push(h("div.mu-disc", t("Disc {n}", { n: x.disc }))); }
    rows.push(h("button.mu-trk", { onclick: () => playTracks(tracks, i, { album: id }) },
      h("span.mu-num", x.track || i + 1),
      h("span.mu-trk-t", h("b", x.title), x.artist && x.artist !== a.artist ? h("span.mu-meta", x.artist) : null),
      h("span.mu-meta", mins(x.duration))));
  });
  put(s.el, 
    h("div.mu-ahead", navCover(a.cover, a.name, 400, ".big"),
      h("div.mu-ainfo", h("div.mu-dtitle", a.name),
        a.artist_id ? h("button.mu-link", { onclick: () => { s.close(); openArtist(a.artist_id, a.artist); } }, a.artist)
          : h("div.mu-sub", a.artist),
        h("div.mu-meta", joinMeta(a.year, t("{n} tracks", { n: tracks.length }), mins(total(tracks)))))),
    h("div.mu-actions",
      h("button.primary", { onclick: () => playTracks(tracks, 0, { album: id }) }, icon("play"), t("Play")),
      h("button", { onclick: () => playTracks(tracks, 0, { album: id, shuffle: true }) }, ic("shuffle"), t("Shuffle")),
      h("button", { onclick: () => openOutputs((ok) => ok && playTracks(tracks, 0, { album: id })) }, icon("speaker"), t("Play on…")),
      h("button", { onclick: () => playTracks(tracks, 0, { album: id, mode: "queue" }) }, ic("list"), t("Queue"))),
    h("div.mu-tracks", rows));
}

async function openArtist(id, name) {
  const s = sheet(name, [h("div.empty", h("span.spin"))]);
  s.el.parentElement.classList.add("mu-sheet");
  let a;
  try { a = await P(`artist/${encodeURIComponent(id)}`); } catch (e) { s.close(); toast(e.message, true); return; }
  const missing = h("div.stack");
  put(s.el, 
    section(t("Your albums"), a.list.length),
    h("div.mu-grid.three", a.list.map((x) => h("button.mu-tile", { onclick: () => { s.close(); openAlbum(x.id); } },
      navCover(x.cover, x.name, 200, ".sq"), h("div.mu-title", x.name), h("div.mu-meta", x.year || "")))),
    section(t("Missing albums")), missing);
  put(missing, h("div.empty", h("span.spin")));
  try {
    const found = (await P(`mb/search?kind=artist&q=${encodeURIComponent(name)}`)).artists || [];
    const pick = found.find((x) => x.name.toLowerCase() === name.toLowerCase()) || found[0];
    if (!pick) { put(missing, h("div.mu-note", t("MusicBrainz does not know this artist."))); return; }
    const d = await P(`mb/discography/${pick.mbid}?name=${encodeURIComponent(name)}`);
    let all = false;
    const draw = () => {
      const list = d.albums.filter((g) => !g.owned && (all || (!g.secondary.length && (g.type === "Album" || g.type === "EP"))));
      put(missing, 
        h("div.mu-row-tools", chip(t("Studio albums"), null, !all, () => { all = false; draw(); }),
          chip(t("Everything"), null, all, () => { all = true; draw(); })),
        list.length ? h("div.mu-card.mu-list", list.map((g) => mbRow(g, name, s)))
          : h("div.mu-note", t("You have every album MusicBrainz lists.")));
    };
    draw();
  } catch (e) { put(missing, h("div.mu-note.err", e.message)); }
}

// ---- discover: MusicBrainz -----------------------------------------------------------------

function mbRow(g, artistName, parent) {
  const type = joinMeta(g.type === "EP" ? "EP" : "", ...g.secondary);
  return h("button.mu-row", { onclick: () => openMbAlbum({ ...g, artist: g.artist || artistName }) },
    mbCover(g.mbid, g.title, ".sm"),
    h("span.mu-row-t", h("b", g.title), h("span.mu-meta", joinMeta(artistName === undefined ? g.artist : "", g.year, type))),
    g.owned ? h("span.chip.ok", icon("check"), t("Owned")) : h("span.chip.accent", t("Missing")));
}

async function openMbArtist(a) {
  const s = sheet(a.name, [h("div.empty", h("span.spin"))]);
  s.el.parentElement.classList.add("mu-sheet");
  try {
    const d = await P(`mb/discography/${a.mbid}?name=${encodeURIComponent(a.name)}`);
    const albums = d.albums.filter((g) => g.type === "Album" && !g.secondary.length);
    const eps = d.albums.filter((g) => g.type === "EP" && !g.secondary.length);
    const other = d.albums.filter((g) => g.secondary.length);
    const block = (title, list) => (list.length ? [section(title, list.length), h("div.mu-card.mu-list", list.map((g) => mbRow(g, a.name)))] : []);
    put(s.el, 
      h("div.mu-meta", joinMeta(a.type, a.country, a.begin, a.disambiguation)),
      h("div.mu-actions", h("button", { onclick: () => openFind({ artist: a.name, album: "", discography: true }) },
        icon("download"), t("Find the discography"))),
      ...block(t("Albums"), albums), ...block(t("EPs"), eps), ...block(t("Other"), other),
      !d.albums.length ? h("div.mu-note", t("No releases found.")) : null);
  } catch (e) { put(s.el, h("div.mu-note.err", e.message)); }
}

async function openMbAlbum(g) {
  const tl = h("div.mu-tracks", h("div.empty", h("span.spin")));
  const s = sheet("", [
    h("div.mu-ahead", mbCover(g.mbid, g.title, ".big"),
      h("div.mu-ainfo", h("div.mu-dtitle", g.title), h("div.mu-sub", g.artist),
        h("div.mu-meta", joinMeta(g.year, g.type)),
        h("div.mu-chips-in", g.owned ? h("span.chip.ok", icon("check"), t("In your library")) : h("span.chip.accent", t("Missing"))))),
    h("div.mu-actions",
      g.owned ? h("button.primary", { onclick: () => { s.close(); openAlbum(g.owned); } }, icon("play"), t("Open in library")) : null,
      h("button" + (g.owned ? "" : ".primary"), { onclick: () => openFind({ artist: g.artist, album: g.title, year: g.year }) },
        icon("search"), t("Search trackers"))),
    tl]);
  s.el.parentElement.classList.add("mu-sheet");
  try {
    const tr = (await P(`mb/tracks/${g.mbid}`)).tracks;
    put(tl, ...(tr.length ? tr.map((x) => h("div.mu-trk.static", h("span.mu-num", x.number || ""),
      h("span.mu-trk-t", h("b", x.title)), h("span.mu-meta", mins(x.length)))) : [h("div.mu-note", t("No track list available."))]));
  } catch (e) { put(tl, h("div.mu-note.err", e.message)); }
}

async function discover() {
  const q = dx.q.trim();
  if (q.length < 2) { dx.error = t("Type at least 2 characters"); drawDiscover(); return; }
  dx.busy = true; dx.error = ""; dx.searched = true; drawDiscover();
  try {
    if (view.dtab === "artist") dx.artists = (await P(`mb/search?kind=artist&q=${encodeURIComponent(q)}`)).artists || [];
    else dx.albums = (await P(`mb/search?kind=album&q=${encodeURIComponent(q)}`)).albums || [];
  } catch (e) { dx.error = e.message; }
  dx.busy = false; drawDiscover();
}

function drawDiscover() {
  if (!els.dres) return;
  put(els.dseg, chip(t("Artists"), null, view.dtab === "artist", () => { view.dtab = "artist"; saveView(); drawDiscover(); if (dx.q.trim().length >= 2) discover(); }),
    chip(t("Albums"), null, view.dtab === "album", () => { view.dtab = "album"; saveView(); drawDiscover(); if (dx.q.trim().length >= 2) discover(); }));
  const out = [];
  if (dx.error) out.push(h("div.mu-note.err", dx.error));
  if (dx.busy) out.push(h("div.empty", h("span.spin")));
  else if (view.dtab === "artist" && dx.artists.length) {
    out.push(h("div.mu-card.mu-list", dx.artists.map((a) => h("button.mu-row", { onclick: () => openMbArtist(a) },
      h("span.mu-ico-sm", icon("users")),
      h("span.mu-row-t", h("b", a.name), h("span.mu-meta", joinMeta(a.type, a.country, a.begin, a.disambiguation))), icon("right")))));
  } else if (view.dtab === "album" && dx.albums.length) {
    out.push(h("div.mu-card.mu-list", dx.albums.map((g) => mbRow(g))));
  } else if (dx.searched && !dx.error) out.push(h("div.empty", t("No results.")));
  else if (!dx.searched) out.push(h("div.mu-hint", icon("search"),
    h("span", t("Search an artist or an album on MusicBrainz, see what you are missing and find it on your trackers."))));
  put(els.dres, ...out);
}

// ---- trackers: search + download ------------------------------------------------------------

function openFind(ctx) {
  let disco = !!ctx.discography;
  const list = h("div");
  const head = h("div.mu-meta");
  const toggle = h("div.mu-row-tools");
  const s = sheet(t("Search trackers"), [head, toggle, list]);
  s.el.parentElement.classList.add("mu-sheet");
  const run = async () => {
    head.textContent = disco ? `${ctx.artist} discography` : joinMeta(ctx.artist, ctx.album);
    put(toggle, chip(t("Album"), null, !disco, () => { disco = false; run(); }),
      chip(t("Discography"), null, disco, () => { disco = true; run(); }));
    put(list, h("div.empty", h("span.spin")));
    try {
      const d = await P(`find?artist=${encodeURIComponent(ctx.artist)}&album=${encodeURIComponent(ctx.album || "")}${disco ? "&discography=1" : ""}`);
      const out = [];
      if (d.disabled) {
        out.push(h("div.mu-card.mu-center", h("div.mu-ico", icon("search")), h("b", t("Search is off")),
          h("p.muted", t("Music search uses the Torrents page. Turn on the Torrents plugin and a tracker first.")),
          h("button.primary", { onclick: () => { s.close(); location.hash = "#/torrents"; } }, t("Open Torrents"))));
      } else {
        for (const p of (d.providers || []).filter((x) => x.ok === false && x.error)) out.push(h("div.mu-warn", icon("wifi"), h("span", `${p.name}: ${p.error}`)));
        if (d.error && !d.results.length) out.push(h("div.mu-note.err", d.error));
        out.push(d.results.length ? h("div.mu-card.mu-res-list", d.results.map((r) => resultRow(r, ctx, disco, s)))
          : (d.error ? null : h("div.empty", t("No results."))));
      }
      put(list, ...out.filter(Boolean));
    } catch (e) { put(list, h("div.mu-note.err", e.message)); }
  };
  run();
}

function resultRow(r, ctx, disco, parent) {
  const srcs = r.sources?.length ? r.sources : (r.source ? [r.source] : []);
  return h("div.mu-res" + (r.match ? "" : ".dim"),
    h("div.mu-res-main", h("div.mu-res-title", r.title),
      h("div.mu-res-meta", r.size ? h("span", fmt.bytes(r.size)) : null,
        h("span.mu-seed", icon("up"), fmt.num(r.seeders || 0)), h("span.mu-leech", icon("down"), fmt.num(r.leechers || 0))),
      h("div.mu-chips-in", (r.hints || []).map((x) => h("span.chip.accent", x)), srcs.slice(0, 3).map((x) => h("span.chip", x)))),
    h("button.primary.mu-dl", { onclick: () => confirmDownload(r, ctx, disco, parent), "aria-label": t("Download") }, icon("download")));
}

function confirmDownload(r, ctx, disco, parent) {
  const artist = h("input", { value: ctx.artist || "", autocomplete: "off", "aria-label": t("Artist") });
  const album = h("input", { value: disco ? "" : (ctx.album || ""), autocomplete: "off", "aria-label": t("Album") });
  const year = h("input", { value: ctx.year || "", inputmode: "numeric", maxlength: 4, "aria-label": t("Year") });
  const dest = h("span");
  const paint = () => {
    const y = /^\d{4}$/.test(year.value.trim()) ? ` (${year.value.trim()})` : "";
    dest.textContent = disco ? `music/${artist.value.trim() || "…"}/…`
      : `music/${artist.value.trim() || "…"}/${(album.value.trim() || "…") + y}`;
  };
  [artist, album, year].forEach((i) => i.addEventListener("input", paint));
  paint();
  const s = sheet(t("Download"), [
    h("div.mu-dtitle.small", r.title),
    h("div.mu-meta", joinMeta(r.size ? fmt.bytes(r.size) : "", r.seeders != null ? t("{n} seeders", { n: r.seeders }) : "")),
    h("label.mu-field", h("span", t("Artist")), artist),
    disco ? null : h("label.mu-field", h("span", t("Album")), album),
    disco ? null : h("label.mu-field", h("span", t("Year")), year),
    h("div.mu-banner", icon("nas"), h("span", t("Saved to {dest}", { dest: "" }), dest)),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      if (!artist.value.trim()) { toast(t("Missing artist"), true); return; }
      await P("download", { body: { title: r.title, artist: artist.value.trim(), album: album.value.trim(), year: year.value.trim(),
        magnet: r.magnet, url: r.url, discography: disco } });
      toast(t("Download started")); s.close(); parent?.close(); setTab("downloads"); await loadDownloads();
    }, e.currentTarget) }, icon("download"), t("Download")),
  ].filter(Boolean));
}

// ---- downloads tab ------------------------------------------------------------------------

function dlState(d) {
  if (d.state === "importing") return t("Adding to the library…");
  if (d.state === "done") return t("In your library");
  if (d.state === "error") return d.error || t("Failed");
  const bits = [d.rate ? `${fmt.bytes(d.rate)}/s` : "", d.eta ? fmt.duration(d.eta) : ""].filter(Boolean);
  return bits.join(" · ") || t("Starting…");
}

function dlRow(d) {
  const active = d.state === "downloading" || d.state === "importing";
  const p = Math.round((d.progress || 0) * 100);
  return h("div.mu-dl-row",
    h("div.mu-res-main", h("div.mu-res-title", d.title),
      active || d.state === "done" ? h("div.progress" + (d.state === "importing" ? ".ind" : ""),
        h("div", { style: { width: d.state === "importing" ? "100%" : `${p}%` } })) : null,
      h("div.mu-note" + (d.state === "error" ? ".err" : ""), d.state === "downloading" ? `${p}% · ${dlState(d)}` : dlState(d))),
    h("button.icon.ghost", { "aria-label": active ? t("Cancel") : t("Remove"), onclick: (e) => act(async () => {
      await P(`downloads/${encodeURIComponent(d.hash)}`, { method: "DELETE" }); await loadDownloads();
    }, e.currentTarget) }, icon(active ? "x" : "trash")));
}

function drawDownloads() {
  if (!els.dl) return;
  put(els.dl, downloads.length ? h("div.mu-card.mu-list", downloads.map(dlRow))
    : h("div.mu-card.mu-center", h("div.mu-ico", icon("download")), h("b", t("No downloads")),
      h("p.muted", t("Albums you download from the trackers show up here."))));
}

// ---- outputs ------------------------------------------------------------------------------

function openOutputs(onDone) {
  const list = h("div.mu-list-plain");
  const pick = (id) => { view.output = id; saveView(); pl.remote = null; s.close(); pollRemote(); drawBar(); emit(); onDone?.(true); };
  const paint = () => {
    const opt = (id, name, sub, ico, extra) => h("div.mu-out-row" + (view.output === id ? ".on" : ""),
      h("button.mu-out-pick", { onclick: () => pick(id) }, h("span.mu-ico-sm", ico),
        h("span.mu-row-t", h("b", name), sub ? h("span.mu-meta", sub) : null), view.output === id ? icon("check") : null), extra);
    put(list, 
      opt(PHONE, t("This phone"), t("Plays in the app"), ic("phone")),
      ...outputs.list.map((o) => opt(o.id, o.name, joinMeta(o.model, o.address, o.needs_pairing ? t("Needs pairing") : ""), icon("speaker"),
        (o.needs_pairing || o.password) ? h("button.mu-setup", { onclick: () => outputSetup(o, paint) }, t("Set up")) : null)),
      outputs.list.length ? null : h("div.mu-note", outputs.loaded ? t("No AirPlay devices found yet.") : t("Looking for devices…")));
  };
  const s = sheet(t("Play on"), [list,
    h("button.wide", { onclick: (e) => act(async () => { await loadOutputs(true); paint(); }, e.currentTarget) }, icon("refresh"), t("Find devices"))],
  { onClose: () => onDone?.(false) });
  s.el.parentElement.classList.add("mu-sheet");
  paint();
  if (!outputs.loaded) loadOutputs().then(paint);
}

function outputSetup(o, repaint) {
  const msg = h("div.mu-note");
  const input = h("input", { type: "text", placeholder: t("PIN"), inputmode: "numeric", autocomplete: "off", hidden: true });
  const go = h("button.primary", {}, t("Pair"));
  let step = null;
  const pair = async () => {
    const r = await P(`outputs/${encodeURIComponent(o.id)}/pair`, { body: { step, value: input.value } });
    msg.textContent = r.message; msg.classList.toggle("err", !!r.error);
    if (r.done) { await loadOutputs(); repaint(); s.close(); toast(r.message); return; }
    step = r.step; input.hidden = !r.input; input.value = "";
    go.textContent = r.step ? t("Continue") : t("Pair");
  };
  go.onclick = (e) => act(pair, e.currentTarget);
  const pw = h("input", { type: "password", placeholder: t("Password"), autocomplete: "off" });
  const s = sheet(o.name, [
    h("div.mu-note", t("Some speakers need pairing (a PIN shown by the device) or a password before they accept music.")),
    msg, input, go,
    h("label.mu-field", h("span", t("Device password")), pw),
    h("button", { onclick: (e) => act(async () => {
      await P(`outputs/${encodeURIComponent(o.id)}/password`, { body: { password: pw.value } });
      toast(t("Saved")); await loadOutputs(); repaint(); s.close();
    }, e.currentTarget) }, t("Save password"))]);
}

// ---- settings -----------------------------------------------------------------------------

async function openSettings() {
  ensureCss(`/static/pages/settings.css?v=${window.CASA_V}`);
  const { renderForm } = await import(`./settings.js?v=${window.CASA_V}`);
  const all = await api("settings");
  const p = all.plugins.find((x) => x.name === "music");
  if (!p) return;
  const schema = p.schema || [];
  const form = renderForm(schema, p.values || {});
  const status = h("div.mu-note");
  const btn = (ico, label, fn) => h("button.mu-wide", { onclick: (e) => act(() => fn(e), e.currentTarget) }, icon(ico), label);
  const s = sheet(t("Music settings"), [
    form.el, status,
    h("div.mu-btns",
      btn("wifi", t("Test Navidrome"), async () => {
        const r = await P("test", { body: {} });
        status.classList.toggle("err", !r.ok);
        status.textContent = r.ok ? t("Connected to Navidrome {v}", { v: r.version || "" }) + (r.music_dir_exists ? "" : " · " + t("The music folder does not exist on the hub")) : r.error;
      }),
      btn("refresh", t("Rescan library"), async () => { await P("rescan", { body: {} }); toast(t("Library scan started")); }),
      btn("speaker", t("Find devices"), async () => { await loadOutputs(true); toast(t("{n} devices found", { n: outputs.list.length })); })),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      const values = form.values();
      for (const f of schema) if (f.type === "password" && !values[f.key]) delete values[f.key];   // empty = keep
      const r = await api("settings/plugins/music", { method: "PUT", body: { values } });
      if (r.error) { toast(r.error, true); return; }
      toast(t("Saved")); s.close(); lib.loaded = false; await loadLibrary();
    }, e.currentTarget) }, t("Save")),
  ], { wide: true });
  s.el.parentElement.classList.add("mu-ssheet");
}

// ---- page ---------------------------------------------------------------------------------

function drawTabs() {
  if (!els.seg) return;
  const n = activeDl().length;
  const tab = (k, label, badge) => h("button" + (view.tab === k ? ".active" : ""), { onclick: () => setTab(k) }, label,
    badge ? h("span.mu-nbadge", fmt.num(badge)) : null);
  put(els.seg, tab("library", t("Library")), tab("discover", t("Discover")), tab("downloads", t("Downloads"), n));
  for (const k of ["library", "discover", "downloads"]) els[k].hidden = view.tab !== k;
}

function setTab(k) { view.tab = k; saveView(); drawTabs(); }

export default {
  async render(el) {
    ensureCss(`/static/pages/music.css?v=${window.CASA_V}`);
    root = h("div.mu");
    els.q = h("input", { type: "search", placeholder: t("Search your music…"), autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), value: query, oninput: (e) => { query = e.target.value; doSearch(); } });
    els.chips = h("div.mu-chips");
    els.body = h("div");
    els.library = h("div", h("div.mu-tools", h("div.mu-search", icon("search"), els.q), h("div.mu-chipwrap", els.chips)), els.body);

    els.dq = h("input", { type: "search", placeholder: t("Artist or album…"), autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), value: dx.q, oninput: (e) => { dx.q = e.target.value; } });
    els.dseg = h("div.mu-chips.wrap");
    els.dres = h("div");
    els.discover = h("div",
      h("form.mu-find", { onsubmit: (e) => { e.preventDefault(); els.dq.blur(); discover(); } },
        h("div.mu-search", icon("search"), els.dq), h("button.primary", { type: "submit" }, t("Search"))),
      h("div.mu-chipwrap", els.dseg), els.dres);
    els.dl = h("div");
    els.downloads = h("div", els.dl);
    els.seg = h("div.seg.mu-seg");
    bar = h("div.mu-bar", { hidden: true });
    root.append(
      h("div.mu-head", h("h1", t("Music")), h("div.spacer"),
        h("button.icon.ghost", { "aria-label": t("Choose where to play"), onclick: () => openOutputs() }, icon("speaker")),
        h("button.icon.ghost", { "aria-label": t("Music settings"), onclick: openSettings }, icon("settings"))),
      els.seg, els.library, els.discover, els.downloads, bar);
    put(el, root);
    drawTabs(); drawLibrary(); drawDiscover(); drawDownloads(); drawBar();

    const onChange = () => { if (barSig() !== lastBarSig) drawBar(); else tickBar(); };
    pl.listeners.add(onChange);
    timers.push(setInterval(tickBar, 500));
    timers.push(setInterval(pollRemote, 2500));
    timers.push(setInterval(() => loadDownloads(), 4000));
    await Promise.all([loadLibrary(), loadDownloads(), loadOutputs()]);
    pollRemote();
    return () => { timers.forEach(clearInterval); timers = []; pl.listeners.delete(onChange); root = null; bar = null; };
  },
};
