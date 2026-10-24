// Music: your Navidrome library, discovery on MusicBrainz, tracker downloads and playback on the house
// speakers (hub/plugins/music/). Route: #/music.
// Tabs: Library | Discover (charts, new releases, search) | Downloads. A mini-player bar sits above the app tab bar. Playback goes either to
// this phone (<audio> on the proxied stream) or to an AirPlay output (the hub streams and keeps the queue).
import { api, h, icon, toast, ensureCss, act, fmt, t, sheet, debounce, locale, registerLocalPlayer, nowBarChanged } from "../app.js";

const P = (path, opts) => api(`p/music/${path}`, opts);
const KEY = "casa.music.view";
const PHONE = "phone";
const LISTS = () => [["newest", t("New")], ["recent", t("Recent")], ["frequent", t("Most played")],
  ["random", t("Random")], ["alphabeticalByName", t("A–Z")], ["artists", t("Artists")], ["genres", t("Genres")],
  ["history", t("Listens")]];

let view = { tab: "library", list: "newest", dtab: "artist", output: PHONE };
try { view = { ...view, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* private mode */ }
const saveView = () => { try { localStorage.setItem(KEY, JSON.stringify(view)); } catch { /* ignore */ } };

let root = null;
let timers = [];
const els = {};
let hist = { recent: [], top: [], stats: null, kind: "track", days: 30, topBusy: false };
let lib = { albums: [], artists: [], genres: [], genre: "", configured: true, error: "", loaded: false, search: null, searchError: "" };
let query = "";
const dx = { q: "", artists: [], albums: [], dzArtists: [], dzAlbums: [], busy: false, searched: false, error: "", fallback: false };
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
  if (src) box.append(h("img", { src, loading: "lazy", alt: "", decoding: "async",
    onload: (e) => e.target.classList.add("in"), onerror: (e) => e.target.remove() }));
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
const emit = () => { pl.listeners.forEach((f) => f()); markPlaying(); nowBarChanged(); };
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

// listen history: seconds really played of the current phone track, reported when it is left (server re-checks the rule)
let ph = null;
function phoneFlush() {
  const x = ph; ph = null;
  if (!x || x.secs < 30) return;
  if (x.secs >= 240 || (x.dur && x.secs >= x.dur * 0.5)) P("listens", { body: { id: x.id, seconds: Math.round(x.secs), output: PHONE } }).catch(() => {});
}
audio.addEventListener("timeupdate", () => {
  if (!ph || audio.paused) return;
  const d = audio.currentTime - ph.last;
  if (d > 0 && d < 1.5) ph.secs += d;
  ph.last = audio.currentTime;
});

function phoneLoad(i) {
  phoneFlush();
  const tr = pl.queue[i];
  if (tr) ph = { id: tr.id, dur: tr.duration || 0, secs: 0, last: 0 };
  if (!tr) { pl.state = "idle"; pl.index = -1; audio.removeAttribute("src"); emit(); return; }
  pl.index = i; pl.error = "";
  audio.src = streamUrl(tr);
  audio.play().catch((e) => {
    if (e.name === "AbortError") return;                       // a newer track replaced this one: not an error
    pl.error = e.name === "NotAllowedError" ? t("Tap play to start the music") : t("This track cannot be played on the phone");
    pl.state = "paused"; emit();
  });
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
audio.addEventListener("ended", () => { if (pl.index + 1 < pl.queue.length) phoneLoad(pl.index + 1); else { phoneFlush(); pl.state = "idle"; emit(); } });
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
async function playTracks(tracks, start = 0, { shuffle = false, album = null, mode = "play", chosen = false } = {}) {
  if (!tracks.length) return;
  picked(tracks[start]?.id);
  // nothing playing anywhere yet: ask where first (the pick is a tap, so the phone may still start audio)
  if (!chosen && mode === "play" && now().state === "idle") {
    let done = false;
    openOutputs((ok) => {
      if (done) return;
      done = true;
      if (ok) playTracks(tracks, start, { shuffle, album, mode, chosen: true });
      else picked(null);
    });
    return;
  }
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
      else if (action === "stop") { phoneFlush(); audio.pause(); audio.removeAttribute("src"); pl.queue = []; pl.index = -1; pl.state = "idle"; emit(); }
      else if (action === "volume") audio.volume = Math.max(0, Math.min(1, extra.value / 100));
      else if (action === "jump") phoneLoad(extra.index);
      emit();
    } else {
      optimistic(action, extra);
      await remote(`player/${action}`, extra);
    }
  } catch (e) { toast(e.message, true); pollRemote(); }
}
// the house-wide "now playing" bar (app.js) shows and controls music playing in this browser
registerLocalPlayer({
  get() {
    const tr = pl.queue[pl.index];
    if (!audio.src || !tr) return null;
    return { state: pl.state, title: tr.title, artist: tr.artist, position: audio.currentTime || 0,
             duration: (Number.isFinite(audio.duration) && audio.duration) || tr.duration || 0,
             artwork: tr.cover ? `/api/p/music/cover/${encodeURIComponent(tr.cover)}?size=100` : "" };
  },
  control(action) {
    if (action === "toggle") { if (audio.paused) audio.play().catch(() => {}); else audio.pause(); }
    else if (action === "next") { if (pl.index + 1 < pl.queue.length) phoneLoad(pl.index + 1); else { phoneFlush(); audio.pause(); pl.state = "idle"; emit(); } }
  },
});
const sendVolume = debounce((v) => control("volume", { value: v }), 120);

/** Show the result of a tap at once; the hub's answer (a moment later) confirms or corrects it. */
function optimistic(action, extra) {
  const r = pl.remote;
  if (!r) return;
  const n = now();
  const at = () => { r.position = n.position; pl.remoteAt = Date.now(); };
  if (action === "pause" || (action === "toggle" && r.state === "playing")) { at(); r.state = "paused"; }
  else if (action === "resume" || action === "toggle") { at(); r.state = "playing"; }
  else if (action === "stop") { r.state = "idle"; r.track = null; r.queue = []; r.index = -1; }
  else if (action === "volume") { r.volume = extra.value; return; }   // the slider already shows it
  else return;
  emit();
}

async function pollRemote() {
  if (isPhone() || !root?.isConnected) return;
  try {
    const r = await P(`player?output=${encodeURIComponent(view.output)}`);
    pl.remote = r; pl.remoteAt = Date.now(); emit();
  } catch { /* output gone or hub busy */ }
}

// ---- which song is playing: highlighted wherever it is listed (rows carry data-tid) ----------

let pickedId = null;
function picked(id) { pickedId = id || null; markPlaying(); }
function markPlaying() {
  const n = now();
  const playing = n.state !== "idle" ? n.track?.id : null;
  if (playing && playing === pickedId) pickedId = null;
  document.querySelectorAll("[data-tid]").forEach((el) => {
    el.classList.toggle("playing", el.dataset.tid === playing);
    el.classList.toggle("picked", el.dataset.tid === pickedId && el.dataset.tid !== playing);
  });
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
    } else if (view.list === "history") {
      const [r, tp, st] = await Promise.all([P("listens/recent?limit=20"), P(`listens/top?kind=${hist.kind}&days=${hist.days}&limit=15`),
        P("listens/stats")]);
      hist.recent = r.listens || []; hist.top = tp.top || []; hist.stats = st;
      lib.configured = true;
    } else if (view.list === "genres" && !lib.genre) {
      lib.genres = (await P("genres")).genres || [];
      lib.configured = true;
    } else if (view.list === "genres") {
      const d = await P(`albums?type=byGenre&genre=${encodeURIComponent(lib.genre)}&size=100`);
      lib.albums = d.albums || []; lib.configured = d.configured !== false;
    } else {
      const d = await P(`albums?type=${encodeURIComponent(view.list)}&size=${view.list === "alphabeticalByName" ? 100 : 36}`);
      lib.albums = d.albums || []; lib.configured = d.configured !== false;
    }
    lib.error = "";
  } catch (e) {
    lib.error = e.message;
    if (view.list === "artists") lib.artists = [];
    else if (view.list === "history") { hist.recent = []; hist.top = []; }
    else if (view.list === "genres" && !lib.genre) lib.genres = [];
    else lib.albums = [];     // never show a list of another view next to an error
  }
  lib.loaded = true;
  drawLibrary();
}

async function loadOutputs(scan = false) {
  try {
    const d = scan ? await P("outputs/scan", { body: {} }) : await P("outputs");
    outputs = { list: d.outputs || [], default: d.default || "", loaded: true };
    outputs.error = "";
  } catch (e) { outputs.loaded = true; outputs.error = e.message; }
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

let dlSig = "";
let dlError = false;
async function loadDownloads() {
  if (document.hidden) return;
  const before = dlError;
  try {
    const d = await P("downloads");
    downloads = Array.isArray(d) ? d : [];
    dlError = false;
  } catch {
    dlError = true;                  // keep what we had; a small note instead of an empty list or a toast every few seconds
  }
  let finished = false;
  for (const d of downloads) {
    if (seen.get(d.hash) && seen.get(d.hash) !== "done" && d.state === "done") finished = true;
    seen.set(d.hash, d.state);
  }
  const sig = JSON.stringify([downloads.map((d) => [d.hash, d.state, Math.round((d.progress || 0) * 100), d.rate ? 1 : 0, d.error || "", d.eta || 0]), dlError]);
  if (sig !== dlSig || before !== dlError) { dlSig = sig; drawDownloads(); }
  drawTabs();
  if (finished) { toast(t("A download was added to your library")); loadLibrary(); }
}
const activeDl = () => downloads.filter((d) => d.state === "downloading" || d.state === "importing");

// ---- library tab ------------------------------------------------------------------------

/** Genre tile: the Deezer picture when there is one, else a coloured gradient; a count under the name. */
function genreTile(name, picture, albums, fn) {
  const box = h("button.mu-gt", { onclick: fn, style: `--mh:${hue(name)}`, "aria-label": name });
  if (picture) box.append(h("img", { src: picture, loading: "lazy", alt: "", decoding: "async",
    onload: (e) => e.target.classList.add("in"), onerror: (e) => e.target.remove() }));
  box.append(h("span.mu-gt-n", h("b", name), albums != null ? h("small", t("{n} albums", { n: albums })) : null));
  return box;
}

/** "Download to the phone": a plain link, so iOS Safari offers to save the file in Files. */
const saveHref = (id, kind) => `/api/p/music/save/${encodeURIComponent(id)}?kind=${kind}`;
const sizeText = (n) => (n > 0 ? fmt.bytes(n) : "");

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
  return h("div.mu-prow", { "data-tid": s.id },
    h("button.mu-prow-main", { onclick: () => playTracks([s], 0) },
      navCover(s.cover, s.album, 100, ".sm"),
      h("span.mu-row-t", h("b", s.title), h("span.mu-meta", joinMeta(s.artist, s.album)))),
    h("span.mu-meta.mu-dur", mins(s.duration)),
    h("a.mu-pv", { href: saveHref(s.id, "track"), download: "", title: t("Download to phone"), "aria-label": t("Download to phone") }, icon("download")));
}

function emptyCard() {
  if (!lib.loaded) return h("div.empty", h("span.spin"));
  if (!lib.configured || (lib.error && !lib.albums.length && !lib.artists.length && !lib.genres.length)) {
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
    view.list = k; lib.genre = ""; query = ""; els.q.value = ""; lib.search = null; saveView(); lib.loaded = false; drawLibrary(); loadLibrary();
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
  if (view.list === "history") { drawHistory(); return; }
  if (view.list === "genres") {
    if (!lib.genre) {
      put(els.body, lib.genres.length ? h("div.mu-grid.mu-ggrid", lib.genres.map((g) => genreTile(g.name, null, g.albums, () => {
        lib.genre = g.name; lib.albums = []; lib.loaded = false; drawLibrary(); loadLibrary();
      }))) : emptyCard());
      return;
    }
    put(els.body, h("div.mu-gback", h("button.icon.ghost", { "aria-label": t("Back"), onclick: () => {
      lib.genre = ""; lib.loaded = false; drawLibrary(); loadLibrary();
    } }, icon("left")), h("h3", lib.genre), h("span.mu-count", t("{n} albums", { n: lib.albums.length }))),
    lib.albums.length ? h("div.mu-grid", lib.albums.map(albumTile)) : emptyCard());
    return;
  }
  if (view.list === "artists") {
    put(els.body, lib.artists.length ? h("div.mu-card.mu-list", lib.artists.map(artistRow)) : emptyCard());
  } else put(els.body, lib.albums.length ? h("div.mu-grid", lib.albums.map(albumTile)) : emptyCard());
}

const listenPlay = (x) => playTracks([{ id: x.song_id, title: x.title, artist: x.artist, album: x.album, album_id: x.album_id,
  cover: x.cover, duration: x.duration, suffix: "" }], 0);

function listenRow(x) {
  return h("div.mu-prow", { "data-tid": x.song_id },
    h("button.mu-prow-main", { onclick: () => listenPlay(x) },
      navCover(x.cover, x.album, 100, ".sm"),
      h("span.mu-row-t", h("b", x.title), h("span.mu-meta", joinMeta(x.artist, x.album)))),
    h("span.mu-meta.mu-dur", fmt.ago(x.played_at)));
}

function topRow(x, rank) {
  const sub = hist.kind === "artist" ? "" : joinMeta(x.artist, hist.kind === "track" ? x.album : "");
  const main = [hist.kind === "artist" ? h("span.mu-rank", rank) : navCover(x.cover, x.name, 100, hist.kind === "artist" ? ".sm.round" : ".sm"),
    h("span.mu-row-t", h("b", x.name), sub ? h("span.mu-meta", sub) : null)];
  const go = hist.kind === "track" ? () => listenPlay({ song_id: x.id, title: x.name, artist: x.artist, album: x.album, album_id: x.album_id, cover: x.cover })
    : hist.kind === "album" ? () => openAlbum(x.id)
      : () => { query = x.name; els.q.value = x.name; doSearch(); };
  return h("div.mu-prow", hist.kind === "track" ? { "data-tid": x.id } : {},
    h("button.mu-prow-main", { onclick: go }, main),
    h("span.mu-meta.mu-dur.mu-plays", `▶ ${fmt.num(x.plays)}`));
}

async function loadTop() {
  hist.topBusy = true; drawHistory();
  try { hist.top = (await P(`listens/top?kind=${hist.kind}&days=${hist.days}&limit=15`)).top || []; } catch { hist.top = []; }
  hist.topBusy = false; drawHistory();
}

function drawHistory() {
  if (!root?.isConnected || view.list !== "history" || query.trim().length >= 2) return;
  const kinds = [["track", t("Songs")], ["album", t("Albums")], ["artist", t("Artists")]];
  const periods = [[7, t("7 days")], [30, t("30 days")], [365, t("Year")], [0, t("All time")]];
  const st = hist.stats;
  put(els.body,
    st?.total ? h("div.mu-note.mu-hstat", t("{n} listens · {h} hours of music", { n: fmt.num(st.total), h: fmt.num(st.hours) })) : null,
    section(t("Recently listened")),
    hist.recent.length ? h("div.mu-card.mu-list", hist.recent.map(listenRow))
      : h("div.mu-card.mu-center", h("p.muted", t("Nothing yet: songs you listen to for at least half, or four minutes, are counted here."))),
    section(t("Most listened")),
    h("div.mu-chips.mu-hchips", kinds.map(([k, l]) => chip(l, null, hist.kind === k, () => { hist.kind = k; loadTop(); }))),
    h("div.mu-chips.mu-hchips", periods.map(([d, l]) => chip(l, null, hist.days === d, () => { hist.days = d; loadTop(); }))),
    hist.topBusy ? h("div.empty", h("span.spin"))
      : hist.top.length ? h("div.mu-card.mu-list", hist.top.map((x, i) => topRow(x, i + 1)))
        : h("div.mu-note", t("No listens in this period.")));
  markPlaying();
}

const doSearch = debounce(async () => {
  const q = query.trim();
  if (q.length < 2) { lib.search = null; drawLibrary(); return; }
  put(els.body, h("div.empty", h("span.spin")));
  lib.searchError = "";
  try { lib.search = await P(`search?q=${encodeURIComponent(q)}`); } catch (e) { lib.search = null; lib.searchError = e.message; }
  if (q !== query.trim()) return;                                  // the box changed meanwhile
  if (lib.searchError) { put(els.body, inlineError(lib.searchError, doSearch)); return; }
  drawLibrary();
}, 350);

// ---- album + artist sheets ------------------------------------------------------------------

async function openAlbum(id) {
  stopPreview();
  const s = sheet("", [h("div.mu-empty-s", h("span.spin"))]);
  s.el.parentElement.classList.add("mu-sheet");
  let a;
  try { a = await P(`album/${encodeURIComponent(id)}`); } catch (e) { sheetError(s, e.message, () => { s.close(); openAlbum(id); }); return; }
  const tracks = a.tracks;
  const multiDisc = new Set(tracks.map((x) => x.disc)).size > 1;
  let lastDisc = 0;
  const rows = [];
  tracks.forEach((x, i) => {
    if (multiDisc && x.disc !== lastDisc) { lastDisc = x.disc; rows.push(h("div.mu-disc", t("Disc {n}", { n: x.disc }))); }
    rows.push(h("div.mu-trkrow",
      { "data-tid": x.id },
      h("button.mu-trk", { onclick: () => playTracks(tracks, i, { album: id }) },
        h("span.mu-num", x.track || i + 1),
        h("span.mu-trk-t", h("b", x.title), x.artist && x.artist !== a.artist ? h("span.mu-meta", x.artist) : null),
        x.plays ? h("span.mu-meta.mu-plays", `▶ ${fmt.num(x.plays)}`) : null,
        h("span.mu-meta", mins(x.duration))),
      h("a.mu-pv.mu-when-view", { href: saveHref(x.id, "track"), download: "",
        title: joinMeta(t("Download to phone"), sizeText(x.size)), "aria-label": t("Download to phone") }, icon("download")),
      h("button.icon.ghost.mu-when-edit.mu-del", { "aria-label": t("Delete from library"), title: t("Delete from library"),
        onclick: () => confirmLibDelete("song", x.id, x.title, () => { s.close(); openAlbum(id); }) }, icon("trash"))));
  });
  const tracksBox = h("div.mu-tracks", rows);
  const sibBox = h("div");
  P(`library/album/${encodeURIComponent(id)}/siblings`).then((r) => {
    if ((r.siblings || []).length < 2 || !sibBox.isConnected) return;
    const list = r.siblings.map((m) => t("CD {n}", { n: m.disc })).join(" + ");
    put(sibBox, h("button.primary.mu-merge", { onclick: () => confirmMergeDiscs(id, r, s) }, ic("list"),
      t("Merge the discs ({list})", { list })));
  }).catch(() => {});
  const manage = h("button.ghost.mu-manage", { onclick: () => {
    const on = tracksBox.classList.toggle("editing");
    manage.replaceChildren(icon(on ? "check" : "edit"), on ? t("Done") : t("Manage"));
  } }, icon("edit"), t("Manage"));
  put(s.el, 
    h("div.mu-ahead", navCover(a.cover, a.name, 400, ".big"),
      h("div.mu-ainfo", h("div.mu-dtitle", a.name),
        a.artist_id ? h("button.mu-link", { onclick: () => { s.close(); openArtist(a.artist_id, a.artist); } }, a.artist)
          : h("div.mu-sub", a.artist),
        h("div.mu-meta", joinMeta(a.year, t("{n} tracks", { n: tracks.length }), mins(total(tracks)))))),
    h("div.mu-actions",
      h("button.primary", { onclick: () => playTracks(tracks, 0, { album: id }) }, icon("play"), t("Play")),
      h("button", { onclick: () => playTracks(tracks, 0, { album: id, shuffle: true }) }, ic("shuffle"), t("Shuffle")),
      h("button", { onclick: () => openOutputs((ok) => ok && playTracks(tracks, 0, { album: id, chosen: true })) }, icon("speaker"), t("Play on…")),
      h("button", { onclick: () => playTracks(tracks, 0, { album: id, mode: "queue" }) }, ic("list"), t("Queue")),
      h("a.btn", { href: saveHref(id, "album"), download: "" }, icon("download"),
        joinMeta(t("Download to phone"), sizeText(a.size)))),
    sibBox,
    h("div.row.mu-manage-row", manage, h("div.spacer"),
      h("button", { onclick: () => openEdit(id, a, s) }, icon("edit"), t("Edit")),
      h("button", { onclick: () => openMergeWith(id, a, s) }, ic("list"), t("Merge with…")),
      h("button.danger", { onclick: () => confirmLibDelete("album", id, a.name, () => { s.close(); loadLibrary(); }) },
        icon("trash"), t("Delete album"))),
    tracksBox);
  markPlaying();
}

/** Run a library change that rewrites files and rescans Navidrome, showing its progress inline. */
async function runJob(statusEl, label, work) {
  statusEl.textContent = label;
  const timer = setInterval(async () => {
    try {
      const j = await P("library/job");
      if (j.phase === "tags") statusEl.textContent = t("Rewriting tags {done}/{total}…", { done: j.done, total: j.total });
      else if (j.phase === "delete") statusEl.textContent = t("Deleting files…");
      else if (j.phase === "scan") statusEl.textContent = t("Updating the library…");
    } catch { /* keep the last text */ }
  }, 1200);
  try { return await work(); } finally { clearInterval(timer); }
}

/** Buttons + inline status + inline error for a sheet that runs a job; the error stays visible in the sheet. */
function jobUi() {
  const status = h("div.mu-note.mu-status", { hidden: true });
  const err = h("div.mu-note.err", { hidden: true, role: "alert" });
  return { status, err,
    async run(btns, label, work) {
      err.hidden = true; status.hidden = false;
      btns.forEach((b) => { b.disabled = true; });
      try { return await runJob(status, label, work); }
      catch (e) { err.textContent = e.message; err.hidden = false; return null; }
      finally { status.hidden = true; btns.forEach((b) => { b.disabled = false; }); }
    } };
}

/** Delete a song or album from the library (the files on the NAS), after showing exactly what goes. */
async function confirmLibDelete(kind, id, name, done) {
  const s = sheet(kind === "album" ? t("Delete album") : t("Delete song"), [h("div.mu-empty-s", h("span.spin"))]);
  let info;
  try { info = await P(`library/${kind}/${encodeURIComponent(id)}/files`); }
  catch (e) { sheetError(s, e.message, () => { s.close(); confirmLibDelete(kind, id, name, done); }); return; }
  const files = info.files || [];
  const ui = jobUi();
  const cancel = h("button", { onclick: () => s.close() }, t("Cancel"));
  const go = h("button.danger", { disabled: !files.length, onclick: async () => {
    const r = await ui.run([go, cancel], t("Updating the library…"),
      () => P(`library/${kind}/${encodeURIComponent(id)}/delete`, { body: { confirm: true } }));
    if (!r) return;
    if (r.listed) {
      ui.err.textContent = t("{n} files were deleted, but Navidrome still lists this. Try Rescan in the Music settings.", { n: r.deleted });
      ui.err.hidden = false; go.hidden = true; cancel.textContent = t("Close"); done?.(); return;
    }
    toast(t("Deleted {n} files", { n: r.deleted }));
    s.close(); done?.();
  } }, icon("trash"), t("Delete for good"));
  put(s.el,
    h("p", h("b", name)),
    files.length ? h("div.mu-note", t("{n} files · {size} will be deleted from the NAS:", { n: files.length, size: sizeText(info.size) }))
      : h("div.mu-note.err", t("The files of this music were not found on the NAS")),
    files.length ? h("pre.mu-paths", files.slice(0, 40).map((f) => f.path).join("\n") + (files.length > 40 ? "\n…" : "")) : null,
    info.missing?.length ? h("div.mu-note.err", t("Not found (left alone): {list}", { list: info.missing.join(", ") })) : null,
    h("div.mu-note", t("This can't be undone.")),
    ui.status, ui.err,
    h("div.row.mu-actions", cancel, go));
}

/** After a tag change: close what is open, reload the library and show the (new) album. */
function reopenAlbum(r, parent) {
  toast(t("Done: {n} files updated", { n: r.changed }));
  parent?.close();
  loadLibrary();
  openAlbum(r.album_id);
}

async function openEdit(id, a, parent) {
  const s = sheet(t("Edit album"), [h("div.mu-empty-s", h("span.spin"))]);
  let info;
  try { info = await P(`library/album/${encodeURIComponent(id)}/tags`); }
  catch (e) { sheetError(s, e.message, () => { s.close(); openEdit(id, a, parent); }); return; }
  const f = info.fields;
  const inp = (k, label, attrs = {}) => {
    const el = h("input", { type: "text", value: f[k].value, placeholder: f[k].mixed ? t("Different values") : "", autocomplete: "off", ...attrs });
    return [el, h("label.mu-field", h("span", label), el)];
  };
  const [iName, lName] = inp("album", t("Album"));
  const [iArtist, lArtist] = inp("albumartist", t("Album artist"));
  const [iYear, lYear] = inp("year", t("Year"), { inputmode: "numeric", maxlength: 4 });
  const [iGenre, lGenre] = inp("genre", t("Genre"));
  const ui = jobUi();
  const cancel = h("button", { onclick: () => s.close() }, t("Cancel"));
  const save = h("button.primary", { onclick: async () => {
    const body = {};
    for (const [k, el] of [["album", iName], ["albumartist", iArtist], ["year", iYear], ["genre", iGenre]]) {
      if (el.value.trim() !== f[k].value) body[k] = el.value.trim();
    }
    if (!Object.keys(body).length) { s.close(); return; }
    const r = await ui.run([save, cancel], t("Rewriting tags…"), () => P(`library/album/${encodeURIComponent(id)}/edit`, { body }));
    if (r) { s.close(); reopenAlbum(r, parent); }
  } }, icon("check"), t("Save"));
  put(s.el, lName, lArtist, h("div.row", lYear, lGenre),
    h("div.mu-note", t("{n} files will be rewritten on the NAS (only the tags, never the audio).", { n: info.count })),
    ui.status, ui.err, h("div.row.mu-actions", cancel, save));
}

async function confirmMergeDiscs(id, sib, parent) {
  const s = sheet(t("Merge the discs"), []);
  const ui = jobUi();
  const cancel = h("button", { onclick: () => s.close() }, t("Cancel"));
  const go = h("button.primary", { onclick: async () => {
    const r = await ui.run([go, cancel], t("Rewriting tags…"), () => P(`library/album/${encodeURIComponent(id)}/merge-discs`, { body: {} }));
    if (r) { s.close(); reopenAlbum(r, parent); }
  } }, t("Merge"));
  put(s.el, h("p", t("These albums become one album “{name}” with {n} discs:", { name: sib.base, n: sib.siblings.length })),
    h("div.mu-card.mu-list", sib.siblings.map((m) => h("div.mu-row", h("span.mu-row-t", h("b", m.name),
      h("span.mu-meta", joinMeta(t("Disc {n}", { n: m.disc }), t("{n} tracks", { n: m.songs }))))))),
    h("div.mu-note", t("Only the tags of the files change (album, disc number); then the library is rescanned.")),
    ui.status, ui.err, h("div.row.mu-actions", cancel, go));
}

async function openMergeWith(id, a, parent) {
  const s = sheet(t("Merge with…"), [h("div.mu-empty-s", h("span.spin"))]);
  let list;
  try { list = ((await P(`artist/${encodeURIComponent(a.artist_id)}`)).list || []).filter((x) => x.id !== id); }
  catch (e) { sheetError(s, e.message, () => { s.close(); openMergeWith(id, a, parent); }); return; }
  if (!list.length) { put(s.el, h("div.mu-note", t("This artist has no other albums."))); return; }
  put(s.el, h("p", t("“{name}” will be added to the album you choose, as one of its discs:", { name: a.name })),
    h("div.mu-card.mu-list", list.map((x) => h("button.mu-row", { onclick: () => confirmMerge(id, a, x, parent, s) },
      navCover(x.cover, x.name, 100, ".sm"),
      h("span.mu-row-t", h("b", x.name), h("span.mu-meta", joinMeta(x.year, t("{n} tracks", { n: x.songs })))), icon("right")))));
}

function confirmMerge(id, a, target, parent, prev) {
  prev.close();
  const s = sheet(t("Merge albums"), []);
  const disc = h("select", h("option", { value: "auto" }, t("Next disc")),
    [1, 2, 3, 4, 5, 6].map((n) => h("option", { value: String(n) }, t("Disc {n}", { n }))));
  const ui = jobUi();
  const cancel = h("button", { onclick: () => s.close() }, t("Cancel"));
  const go = h("button.primary", { onclick: async () => {
    const r = await ui.run([go, cancel], t("Rewriting tags…"), () => P(`library/album/${encodeURIComponent(id)}/merge`,
      { body: { into: target.id, as_disc: disc.value === "auto" ? "auto" : Number(disc.value) } }));
    if (r) { s.close(); reopenAlbum(r, parent); }
  } }, t("Merge"));
  put(s.el, h("p", t("“{a}” becomes part of “{b}”.", { a: a.name, b: target.name })),
    h("label.mu-field", h("span", t("Position")), disc),
    h("div.mu-note", t("Only the tags of the files change (album, disc number); then the library is rescanned.")),
    ui.status, ui.err, h("div.row.mu-actions", cancel, go));
}

async function openArtist(id, name) {
  stopPreview();
  const s = sheet(name, [h("div.mu-empty-s", h("span.spin"))]);
  s.el.parentElement.classList.add("mu-sheet");
  let a;
  try { a = await P(`artist/${encodeURIComponent(id)}`); } catch (e) { sheetError(s, e.message, () => { s.close(); openArtist(id, name); }); return; }
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

// ---- discover: charts and new releases (Deezer, Apple Music) + search ---------------------------------
// Default view = rails. Searching shows Deezer results (MusicBrainz when Deezer is unreachable); the artist sheet
// can open the full MusicBrainz discography (owned / missing) and the tracker search.

const enc = encodeURIComponent;
const RAILS = () => [
  { key: "top_it", title: t("Top in Italy"), kind: "album" },
  { key: "chart_albums", title: t("Albums of the moment"), kind: "album" },
  { key: "chart_artists", title: t("Artists of the moment"), kind: "artist" },
  { key: "new", title: t("New releases"), kind: "album" },
  { key: "chart_tracks", title: t("Songs of the moment"), kind: "track" },
];
const RAIL_TTL = 5 * 60 * 1000;
const dsc = { rails: {}, errors: {}, busy: {}, at: {}, genres: null, genre: "", genreData: null, genreErr: "", genreBusy: false, gAll: false };
els.rails = {};
let searchToken = 0;

const shortNum = (n) => {
  try { return new Intl.NumberFormat(locale(), { notation: "compact", maximumFractionDigits: 1 }).format(n); } catch { return fmt.num(n); }
};

/** Compact inline error with a retry button: never a toast, never taller than a few lines. */
function inlineError(message, retry) {
  return h("div.mu-inerr", { role: "alert" }, icon("alert"), h("span", message || t("Something went wrong")),
    retry ? h("button.mu-mini", { onclick: (e) => { e.currentTarget.disabled = true; retry(); } }, t("Try again")) : null);
}

// -- preview player: one 30 s clip at a time -----------------------------------------------------

const pv = new Audio();
pv.preload = "none";
let pvCur = null;

function pvPaint(btn, on) {
  if (!btn) return;
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.replaceChildren(icon(on ? "pause" : "play"));
  const row = btn.closest(".mu-trk, .mu-prow");
  if (row) { row.classList.toggle("playing", on); if (!on) row.style.removeProperty("--p"); }
}
function stopPreview() {
  const cur = pvCur;
  pvCur = null;
  pvPaint(cur?.btn, false);
  if (pv.getAttribute("src")) { pv.pause(); pv.removeAttribute("src"); pv.load(); }
}
function pvBad(btn) {
  pvPaint(btn, false);
  btn.classList.add("bad");
  btn.disabled = true;
  btn.title = t("Preview unavailable");
}
pv.addEventListener("ended", stopPreview);
pv.addEventListener("timeupdate", () => {
  const row = pvCur?.btn.closest(".mu-trk, .mu-prow");
  if (row && pv.duration) row.style.setProperty("--p", `${Math.min(100, (pv.currentTime / pv.duration) * 100)}%`);
});
pv.addEventListener("error", () => { if (pvCur && pv.getAttribute("src")) { const b = pvCur.btn; pvCur = null; pvBad(b); } });

function togglePreview(tr, btn) {
  if (pvCur?.btn === btn) { stopPreview(); return; }
  stopPreview();
  if (isPhone() && pl.state === "playing") audio.pause();       // the library player and a clip never overlap
  pvCur = { btn };
  pv.src = tr.preview;
  pvPaint(btn, true);
  pv.play().catch((e) => { if (e.name !== "AbortError" && pvCur?.btn === btn) { pvCur = null; pvBad(btn); } });
}

function previewBtn(tr) {
  if (!tr.preview) return h("span.mu-pv.off", { title: t("No preview"), "aria-hidden": "true" }, icon("play"));
  const b = h("button.mu-pv", { "aria-label": t("Preview"), "aria-pressed": "false",
    onclick: (e) => { e.stopPropagation(); togglePreview(tr, b); } }, icon("play"));
  return b;
}

// -- tiles and rails -------------------------------------------------------------------------------

function dTile(a, withYear = false, hideArtist = false) {
  return h("button.mu-tile.mu-dt", { onclick: () => openDzAlbum(a), "aria-label": joinMeta(a.title, a.artist) },
    h("div.mu-covwrap", cover(a.cover, a.title),
      a.owned ? h("span.mu-badge", icon("check"), h("span", t("In your library"))) : null),
    h("div.mu-title", a.title),
    h("div.mu-meta", joinMeta(hideArtist ? "" : a.artist, withYear ? a.year : "")));
}

function aTile(a) {
  return h("button.mu-tile.mu-at", { onclick: () => openDzArtist(a.id, a.name), "aria-label": a.name },
    cover(a.picture, a.name, ".round"),
    h("div.mu-title", a.name),
    h("div.mu-meta", a.fans ? t("{n} fans", { n: shortNum(a.fans) }) : " "));
}

function trackRow(tr, rank) {
  return h("div.mu-prow",
    h("button.mu-prow-main", { onclick: () => tr.album_id && openDzAlbum({ id: tr.album_id, title: tr.album, artist: tr.artist }),
      "aria-label": joinMeta(tr.title, tr.artist) },
    rank ? h("span.mu-num", rank) : null,
    cover(tr.cover, tr.album || tr.title, ".sm"),
    h("span.mu-row-t", h("b", tr.title), h("span.mu-meta", tr.artist)),
    tr.owned ? h("span.mu-own", { title: t("In your library") }, icon("check")) : null),
    h("span.mu-meta.mu-dur", mins(tr.duration)),
    previewBtn(tr));
}

const railHead = (title, extra) => h("div.mu-rhead", h("h3", title), extra || null);
const skelTiles = (kind, n = 5) => Array.from({ length: n }, () =>
  h("div.mu-tile.mu-skel", h("div.mu-cov" + (kind === "artist" ? ".round" : "")), h("div.mu-sk-l"), h("div.mu-sk-s")));
const skelRows = () => h("div.mu-card.mu-list", Array.from({ length: 4 }, () =>
  h("div.mu-prow.mu-skel", h("div.mu-cov.sm"), h("div.mu-row-t", h("div.mu-sk-l"), h("div.mu-sk-s")))));

function paintRail(key) {
  const box = els.rails[key];
  const def = RAILS().find((r) => r.key === key);
  if (!box || !def) return;
  const items = dsc.rails[key];
  box.hidden = false;
  if (items?.length) {
    const shown = def.kind === "track" ? items.slice(0, 8) : items;
    put(box, railHead(def.title),
      def.kind === "track" ? h("div.mu-card.mu-list", shown.map((x, i) => trackRow(x, i + 1)))
        : h("div.mu-rail" + (def.kind === "artist" ? ".round" : ""), shown.map((x) => (def.kind === "artist" ? aTile(x) : dTile(x)))));
  } else if (dsc.busy[key]) {
    put(box, railHead(def.title), def.kind === "track" ? skelRows() : h("div.mu-rail" + (def.kind === "artist" ? ".round" : ""), skelTiles(def.kind)));
  } else if (dsc.errors[key]) {
    put(box, railHead(def.title), inlineError(dsc.errors[key], () => loadRail(key, true)));
  } else box.hidden = !!items;          // loaded and empty: nothing to show
}

async function loadRail(key, force = false) {
  if (dsc.busy[key]) return;
  if (!force && dsc.rails[key] && Date.now() - dsc.at[key] < RAIL_TTL) return;
  dsc.busy[key] = true; dsc.errors[key] = "";
  paintRail(key);
  try {
    const d = await P(`discover?rails=${key}`);
    const r = (d.rails || []).find((x) => x.key === key);
    if (r) { dsc.rails[key] = r.items || []; dsc.at[key] = Date.now(); }
    dsc.errors[key] = r ? "" : (d.errors?.[key] || t("Something went wrong"));
  } catch (e) { dsc.errors[key] = e.message; }
  dsc.busy[key] = false;
  paintRail(key);
}

async function loadGenres() {
  if (dsc.genres) return;
  try { dsc.genres = (await P("discover/genres")).genres || []; } catch { dsc.genres = []; }   // no tiles; the rails still work
  paintGenreTiles();
}

function paintGenreTiles() {
  if (!els.gsec) return;
  els.gsec.hidden = !dsc.genres?.length || !!dsc.genre;
  if (els.gsec.hidden) return;
  const tiles = dsc.genres.map((g) => genreTile(g.name, g.picture, null, () => pickGenre(g.id)));
  put(els.gsec, railHead(t("Genres"), h("button.ghost.mu-mini", { onclick: () => { dsc.gAll = !dsc.gAll; paintGenreTiles(); } },
    dsc.gAll ? t("Show less") : t("See all"))),
    dsc.gAll ? h("div.mu-grid.mu-ggrid", tiles) : h("div.mu-rail.mu-grail", tiles));
}

async function pickGenre(id) {
  dsc.genre = id; dsc.genreData = null; dsc.genreErr = "";
  paintGenreTiles();
  els.railsWrap.hidden = !!id;
  els.genreBox.hidden = !id;
  if (!id) { RAILS().forEach((r) => { paintRail(r.key); loadRail(r.key); }); return; }
  window.scrollTo?.({ top: 0 });
  const mine = id;
  dsc.genreBusy = true; paintGenre();
  try { dsc.genreData = await P(`discover/genre/${enc(id)}`); } catch (e) { dsc.genreErr = e.message; }
  if (dsc.genre !== mine) return;
  dsc.genreBusy = false; paintGenre();
}

function paintGenre() {
  const g = dsc.genres?.find((x) => x.id === dsc.genre);
  const head = h("div.mu-gback", h("button.icon.ghost", { "aria-label": t("Back"), onclick: () => pickGenre("") }, icon("left")),
    h("h3", g?.name || t("Charts")));
  if (dsc.genreBusy) { put(els.genreBox, head, h("div.mu-grid", skelTiles("album", 6))); return; }
  if (dsc.genreErr) { put(els.genreBox, head, inlineError(dsc.genreErr, () => pickGenre(dsc.genre))); return; }
  const d = dsc.genreData || {};
  const part = (key, title, body) => (d.errors?.[key] ? [railHead(title), inlineError(d.errors[key], () => pickGenre(dsc.genre))]
    : body.length ? [railHead(title), ...body] : []);
  const out = [
    ...part("albums", t("Top albums"), d.albums?.length ? [h("div.mu-grid", d.albums.map((a) => dTile(a, true)))] : []),
    ...part("artists", t("Top artists"), d.artists?.length ? [h("div.mu-rail.round", d.artists.map(aTile))] : []),
    ...part("tracks", t("Top songs"), d.tracks?.length ? [h("div.mu-card.mu-list", d.tracks.slice(0, 15).map((x, i) => trackRow(x, i + 1)))] : []),
  ];
  put(els.genreBox, head, out.length ? out : h("div.mu-note", t("No results.")));
}

function ensureDiscover() {
  loadGenres();
  if (dsc.genre) return;
  RAILS().forEach((r) => { paintRail(r.key); loadRail(r.key); });
}

// -- search ----------------------------------------------------------------------------------------

async function discover() {
  const q = dx.q.trim();
  if (q.length < 2) { dx.error = t("Type at least 2 characters"); dx.searched = true; drawDiscover(); return; }
  const mine = ++searchToken;
  dx.busy = true; dx.error = ""; dx.searched = true; dx.fallback = false; drawDiscover();
  try {
    const d = await P(`discover/search?q=${enc(q)}`);
    if (mine !== searchToken) return;
    dx.dzArtists = d.artists || []; dx.dzAlbums = d.albums || [];
  } catch {
    // Deezer is unreachable: the MusicBrainz search still works
    try {
      if (view.dtab === "artist") dx.artists = (await P(`mb/search?kind=artist&q=${enc(q)}`)).artists || [];
      else dx.albums = (await P(`mb/search?kind=album&q=${enc(q)}`)).albums || [];
      dx.fallback = true; dx.dzArtists = []; dx.dzAlbums = [];
    } catch (e2) { dx.error = e2.message; }
  }
  if (mine !== searchToken) return;
  dx.busy = false; drawDiscover();
}

function clearSearch() {
  searchToken++;
  Object.assign(dx, { q: "", searched: false, busy: false, error: "", fallback: false, dzArtists: [], dzAlbums: [] });
  if (els.dq) els.dq.value = "";
  drawDiscover();
}

function drawDiscover() {
  if (!els.dres) return;
  const s = dx.searched;
  els.home.hidden = s; els.dres.hidden = !s; els.dseg.parentElement.hidden = !s;
  els.dclear.hidden = !dx.q;
  if (!s) { ensureDiscover(); return; }
  const tab = (k, label) => chip(label, null, view.dtab === k, () => {
    view.dtab = k; saveView();
    if (dx.fallback && dx.q.trim().length >= 2) discover(); else drawDiscover();
  });
  put(els.dseg, tab("artist", t("Artists")), tab("album", t("Albums")));
  const out = [];
  if (dx.error) out.push(inlineError(dx.error, () => discover()));
  if (dx.busy) out.push(h("div.mu-grid", skelTiles(view.dtab === "artist" ? "artist" : "album", 6)));
  else if (dx.fallback) {
    out.push(h("div.mu-warn", icon("wifi"), h("span", t("The charts service is not reachable: showing MusicBrainz results."))));
    if (view.dtab === "artist" && dx.artists.length) {
      out.push(h("div.mu-card.mu-list", dx.artists.map((a) => h("button.mu-row", { onclick: () => openMbArtist(a) },
        h("span.mu-ico-sm", icon("users")),
        h("span.mu-row-t", h("b", a.name), h("span.mu-meta", joinMeta(a.type, a.country, a.begin, a.disambiguation))), icon("right")))));
    } else if (view.dtab === "album" && dx.albums.length) out.push(h("div.mu-card.mu-list", dx.albums.map((g) => mbRow(g))));
    else if (!dx.error) out.push(h("div.empty", t("No results.")));
  } else if (!dx.error) {
    const list = view.dtab === "artist" ? dx.dzArtists : dx.dzAlbums;
    if (!list.length) out.push(h("div.empty", t("No results.")));
    else if (view.dtab === "artist") out.push(h("div.mu-grid.three.mu-agrid", list.map(aTile)));
    else out.push(h("div.mu-grid", list.map((a) => dTile(a, true))));
  }
  put(els.dres, ...out);
}

// -- album + artist sheets (Deezer data) --------------------------------------------------------------

function sheetError(s, message, retry, extra) {
  put(s.el, inlineError(message, retry), extra || null);
}

const heroBg = (src) => (src ? h("div.mu-dbg", { style: { backgroundImage: `url("${src}")` } }) : null);

async function playOwned(id) {
  const a = await P(`album/${enc(id)}`);
  await playTracks(a.tracks, 0, { album: id });
}

function openDzAlbum(ref) {
  stopPreview();
  const s = sheet("", [h("div.mu-skel-sheet", h("div.mu-cov.big"), h("div.mu-sk-l"), h("div.mu-sk-s"))], { onClose: stopPreview });
  s.el.parentElement.classList.add("mu-sheet");
  const load = async () => {
    put(s.el, h("div.mu-empty-s", h("span.spin")));
    try {
      const id = ref.id || (await P(`discover/resolve?artist=${enc(ref.artist || "")}&title=${enc(ref.title || "")}`)).id;
      paintDzAlbum(s, await P(`discover/album/${enc(id)}`));
    } catch (e) {
      sheetError(s, e.message, load, ref.title ? h("div.mu-actions",
        h("button.primary", { onclick: () => openFind({ artist: ref.artist || "", album: ref.title, year: ref.year }) },
          icon("search"), t("Search trackers"))) : null);
    }
  };
  load();
}

function paintDzAlbum(s, a) {
  const tracks = a.tracks || [];
  const rows = tracks.map((x, i) => {
    const b = previewBtn(x);
    const row = h("div.mu-trk" + (x.preview ? ".can" : ""), { onclick: () => x.preview && b.click() },
      h("span.mu-num", i + 1),
      h("span.mu-trk-t", h("b", x.title), x.artist && x.artist !== a.artist ? h("span.mu-meta", x.artist) : null),
      h("span.mu-meta.mu-dur", mins(x.duration)), b);
    return row;
  });
  const hasPrev = tracks.some((x) => x.preview);
  put(s.el,
    h("div.mu-dhero", heroBg(a.cover),
      h("div.mu-ahead", cover(a.cover, a.title, ".big"),
        h("div.mu-ainfo",
          h("div.mu-dtitle", a.title),
          a.artist_id ? h("button.mu-link", { onclick: () => { s.close(); openDzArtist(a.artist_id, a.artist); } }, a.artist)
            : h("div.mu-sub", a.artist),
          h("div.mu-meta", joinMeta(a.year, tracks.length ? t("{n} tracks", { n: tracks.length }) : "", mins(a.duration || total(tracks)))),
          a.label ? h("div.mu-meta", a.label) : null))),
    h("div.mu-chips-in",
      a.owned ? h("span.chip.ok", icon("check"), t("In your library")) : null,
      ...(a.genres || []).slice(0, 4).map((g) => h("span.chip", g))),
    h("div.mu-actions",
      a.owned ? h("button.primary", { onclick: (e) => act(() => playOwned(a.owned), e.currentTarget) }, icon("play"), t("Play")) : null,
      h("button" + (a.owned ? "" : ".primary"), { onclick: () => openFind({ artist: a.artist, album: a.title, year: a.year }) },
        icon("search"), t("Search trackers")),
      a.owned ? h("button", { onclick: () => { s.close(); openAlbum(a.owned); } }, icon("music"), t("Open in library")) : null,
      a.link ? h("a.btn.mu-ext", { href: a.link, target: "_blank", rel: "noopener" }, icon("link"), t("Open on Deezer")) : null),
    hasPrev ? h("div.mu-note", t("Tap a track to hear a 30 second preview.")) : null,
    h("div.mu-tracks", rows.length ? rows : h("div.mu-note", t("No track list available."))));
}

async function openDzArtist(id, name) {
  stopPreview();
  const s = sheet(name || "", [h("div.mu-empty-s", h("span.spin"))], { onClose: stopPreview });
  s.el.parentElement.classList.add("mu-sheet");
  const load = async () => {
    put(s.el, h("div.mu-empty-s", h("span.spin")));
    try { paintDzArtist(s, await P(`discover/artist/${enc(id)}`)); }
    catch (e) { sheetError(s, e.message, load); }
  };
  load();
}

function paintDzArtist(s, a) {
  const kinds = { album: [], small: [] };
  for (const x of a.albums || []) (["album", "compile"].includes(x.type) ? kinds.album : kinds.small).push(x);
  let tab = kinds.album.length || !kinds.small.length ? "album" : "small";
  let allTop = false;
  const top = h("div"), grid = h("div"), tabs = h("div.mu-row-tools");
  const err = (k) => (a.errors?.[k] ? inlineError(a.errors[k]) : null);
  const paintTop = () => {
    const list = a.top || [];
    put(top, section(t("Top songs")),
      list.length ? h("div.mu-card.mu-list", (allTop ? list : list.slice(0, 5)).map((x, i) => trackRow(x, i + 1))) : (err("top") || h("div.mu-note", t("No results."))),
      list.length > 5 ? h("button.mu-link", { onclick: () => { allTop = !allTop; stopPreview(); paintTop(); } }, allTop ? t("Show less") : t("Show more")) : null);
  };
  const paintAlbums = () => {
    const list = kinds[tab];
    put(tabs, chip(t("Albums"), kinds.album.length, tab === "album", () => { tab = "album"; paintAlbums(); }),
      chip(t("Singles and EPs"), kinds.small.length, tab === "small", () => { tab = "small"; paintAlbums(); }));
    put(grid, list.length ? h("div.mu-grid.three", list.map((x) => dTile({ ...x, artist: a.name }, true, true)))
      : (err("albums") || h("div.mu-note", t("No results."))));
  };
  paintTop(); paintAlbums();
  put(s.el,
    h("div.mu-dhero", heroBg(a.picture),
      h("div.mu-ahead", cover(a.picture, a.name, ".big.round"),
        h("div.mu-ainfo", h("div.mu-dtitle", a.name),
          a.fans ? h("div.mu-meta", t("{n} fans", { n: fmt.num(a.fans) })) : null,
          a.albums_count ? h("div.mu-meta", t("{n} albums", { n: a.albums_count })) : null))),
    h("div.mu-actions",
      h("button.primary", { onclick: () => openFind({ artist: a.name, album: "", discography: true }) }, icon("search"), t("Find the discography")),
      h("button", { onclick: (e) => act(() => openMbByName(a.name), e.currentTarget) }, ic("list"), t("What am I missing?")),
      a.link ? h("a.btn.mu-ext", { href: a.link, target: "_blank", rel: "noopener" }, icon("link"), t("Open on Deezer")) : null),
    top,
    section(t("Discography")), tabs, grid,
    a.related?.length ? [section(t("Similar artists")), h("div.mu-rail.round", a.related.map((x) => aTile(x)))]
      : (a.errors?.related ? [section(t("Similar artists")), inlineError(a.errors.related)] : null));
  // album taps open on top of this sheet, so closing the album comes back here
  s.el.querySelectorAll(".mu-rail.round .mu-at").forEach((el) => { el.addEventListener("click", () => s.close(), true); });
}

/** MusicBrainz discography (owned / missing) of an artist known only by name. */
async function openMbByName(name) {
  const found = (await P(`mb/search?kind=artist&q=${enc(name)}`)).artists || [];
  const pick = found.find((x) => x.name.toLowerCase() === name.toLowerCase()) || found[0];
  if (!pick) { toast(t("MusicBrainz does not know this artist."), true); return; }
  openMbArtist(pick);
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
    d.state === "error" && h("button.icon.ghost", { "aria-label": t("Try again"), title: t("Try again"), onclick: (e) => act(async () => {
      await P(`downloads/${encodeURIComponent(d.hash)}/retry`, { body: {} }); await loadDownloads();
    }, e.currentTarget) }, icon("refresh")),
    h("button.icon.ghost", { "aria-label": active ? t("Cancel") : t("Remove"), onclick: (e) => act(async () => {
      if (active) { await P(`downloads/${encodeURIComponent(d.hash)}`, { method: "DELETE" }); await loadDownloads(); }
      else await openRemove(d);
    }, e.currentTarget) }, icon(active ? "x" : "trash")));
}

/** Remove a finished or failed entry: from the list only, or together with its files (path and size shown). */
async function openRemove(d) {
  const hash = encodeURIComponent(d.hash);
  const info = await P(`downloads/${hash}/info`);
  const done = async (fn) => { await fn(); s.close(); await loadDownloads(); };
  const nothing = !info.payload && !info.albums.length;
  const where = (x) => h("div.mu-delbox", h("code", x.path), h("span.mu-meta", sizeText(x.size) || t("Empty")));
  const s = sheet(t("Remove download"), [
    h("div.mu-res-title", d.title),
    info.payload ? h("div.stack", h("div.mu-note", t("Files still in the downloads folder:")), where(info.payload)) : null,
    info.albums.length ? h("div.stack", h("div.mu-note", t("Albums in your library:")), ...info.albums.map(where)) : null,
    h("div.mu-actions.col",
      h("button", { onclick: (e) => act(() => done(() => P(`downloads/${hash}`, { method: "DELETE" })), e.currentTarget) },
        icon("x"), t("Remove from list")),
      info.payload ? h("button.danger", { onclick: (e) => act(() => done(() => P(`downloads/${hash}?files=1`, { method: "DELETE" })), e.currentTarget) },
        icon("trash"), t("Remove and delete the files")) : null,
      info.albums.length ? h("button.danger", { onclick: (e) => act(() => done(() => P(`downloads/${hash}/library-delete`, { body: {} })), e.currentTarget) },
        icon("trash"), t("Delete from library")) : null),
    info.albums.length ? h("div.mu-note", t("The albums are deleted from the music folder and the library is rescanned.")) : null,
    nothing ? h("div.mu-note", t("No files are left for this download.")) : null,
  ]);
}

function drawDownloads() {
  if (!els.dl) return;
  put(els.dl, dlError ? h("div.mu-warn", icon("wifi"), h("span", t("Cannot refresh the downloads right now."))) : null,
    downloads.length ? h("div.mu-card.mu-list", downloads.map(dlRow))
    : h("div.mu-card.mu-center", h("div.mu-ico", icon("download")), h("b", t("No downloads")),
      h("p.muted", t("Albums you download from the trackers show up here."))));
}

// ---- outputs ------------------------------------------------------------------------------

function openOutputs(onDone) {
  const list = h("div.mu-list-plain");
  let decided = false;
  const pick = (id) => {
    view.output = id; saveView(); pl.remote = null; decided = true;
    onDone?.(true);           // still inside the tap: the phone is allowed to start audio
    s.close(); pollRemote(); drawBar(); emit();
  };
  const paint = () => {
    const opt = (id, name, sub, ico, extra) => h("div.mu-out-row" + (view.output === id ? ".on" : ""),
      h("button.mu-out-pick", { onclick: () => pick(id) }, h("span.mu-ico-sm", ico),
        h("span.mu-row-t", h("b", name), sub ? h("span.mu-meta", sub) : null), view.output === id ? icon("check") : null), extra);
    put(list, 
      opt(PHONE, t("This phone"), t("Plays in the app"), ic("phone")),
      ...outputs.list.map((o) => opt(o.id, o.name, joinMeta(o.model, o.address, o.needs_pairing ? t("Needs pairing") : ""), icon("speaker"),
        (o.needs_pairing || o.password) ? h("button.mu-setup", { onclick: () => outputSetup(o, paint) }, t("Set up")) : null)),
      outputs.list.length ? null : h("div.mu-note" + (outputs.error ? ".err" : ""),
        outputs.error || (outputs.loaded ? t("No AirPlay devices found yet.") : t("Looking for devices…"))));
  };
  const s = sheet(t("Play on"), [list,
    h("button.wide", { onclick: (e) => act(async () => { await loadOutputs(true); paint(); }, e.currentTarget) }, icon("refresh"), t("Find devices"))],
  { onClose: () => { if (!decided) onDone?.(false); } });
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

let tabSig = "";
function drawTabs() {
  if (!els.seg) return;
  const n = activeDl().length;
  for (const k of ["library", "discover", "downloads"]) els[k].hidden = view.tab !== k;
  if (tabSig === `${view.tab}|${n}`) return;                      // polling must not rebuild the tab bar
  tabSig = `${view.tab}|${n}`;
  const tab = (k, label, badge) => h("button" + (view.tab === k ? ".active" : ""), { onclick: () => setTab(k) }, label,
    badge ? h("span.mu-nbadge", fmt.num(badge)) : null);
  put(els.seg, tab("library", t("Library")), tab("discover", t("Discover")), tab("downloads", t("Downloads"), n));
}

function setTab(k) { view.tab = k; saveView(); drawTabs(); if (k === "discover") drawDiscover(); }

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
      "aria-label": t("Search"), value: dx.q, oninput: (e) => {
        dx.q = e.target.value;
        els.dclear.hidden = !dx.q;
        if (!dx.q.trim() && dx.searched) clearSearch();          // emptied: back to the charts
      } });
    els.dclear = h("button.mu-clear", { type: "button", hidden: !dx.q, "aria-label": t("Clear"), onclick: () => { clearSearch(); els.dq.focus(); } }, icon("x"));
    els.dseg = h("div.mu-chips");
    els.dres = h("div");
    els.gsec = h("section.mu-rbox", { hidden: true });
    els.genreBox = h("div", { hidden: true });
    els.railsWrap = h("div");
    for (const r of RAILS()) { els.rails[r.key] = h("section.mu-rbox"); els.railsWrap.append(els.rails[r.key]); }
    els.home = h("div", els.gsec, els.genreBox, els.railsWrap);
    els.discover = h("div",
      h("form.mu-find", { onsubmit: (e) => { e.preventDefault(); els.dq.blur(); discover(); } },
        h("div.mu-search", icon("search"), els.dq, els.dclear), h("button.primary", { type: "submit" }, t("Search"))),
      h("div.mu-chipwrap", els.dseg), els.dres, els.home);
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
    drawTabs(); drawLibrary(); drawDownloads(); drawBar();
    els.dclear.hidden = !dx.q;
    if (view.tab === "discover") drawDiscover();

    const onChange = () => { if (barSig() !== lastBarSig) drawBar(); else tickBar(); };
    pl.listeners.add(onChange);
    timers.push(setInterval(tickBar, 500));
    timers.push(setInterval(pollRemote, 2500));
    timers.push(setInterval(() => loadDownloads(), 4000));
    await Promise.all([loadLibrary(), loadDownloads(), loadOutputs()]);
    pollRemote();
    return () => { timers.forEach(clearInterval); timers = []; pl.listeners.delete(onChange); stopPreview(); root = null; bar = null; };
  },
};
