// Media: the "Sottotitoli" sheet (list, online search, adapt / manual sync, translate) and the
// library-scan card. Loaded on demand by media.js. Routes: hub/plugins/media/__init__.py (/subtitles/*).
import { t, api, h, icon, toast, sheet, act, fmt, errorCard } from "../app.js";

const P = (path, opts) => api(`p/media/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const FLAG = { it: "🇮🇹", en: "🇬🇧", fr: "🇫🇷", es: "🇪🇸", de: "🇩🇪", pt: "🇵🇹" };
const flag = (l) => FLAG[l] || (l ? l.toUpperCase() : "?");
const LANGNAME = () => ({ it: t("Italian"), en: t("English"), es: t("Spanish"), fr: t("French"), de: t("German"), pt: t("Portuguese") });
const loading = () => h("div.empty", h("div.spin"));
const num = (n) => Number(n || 0).toLocaleString();

const ORIGIN = () => ({ in_sync: t("in sync"), adapted: t("timing adjusted"), unverified: t("not verified"), found: t("found") });

/** Open the subtitles sheet for one library video. `meta`: {file, title, year, imdb_id}. */
export function openSubsSheet(meta, { onChange } = {}) {
  const s = sheet(`${t("Subtitles")} · ${meta.title || ""}`, [loading()], { wide: true, onClose: () => { closed = true; onChange?.(); } });
  let closed = false;
  let data = null;
  let results = null;       // last online search: {candidates}
  let resLang = "it";
  let adapting = null;      // name of the sidecar whose adapter panel is open
  let pollTimer = null;
  const body = h("div.stack", { style: { gap: "14px" } }, loading());
  fill(s.el, body);

  const base = () => ({ file: meta.file, title: meta.title, year: meta.year, imdb_id: meta.imdb_id });
  const call = (path, extra = {}) => P(`subtitles/${path}`, { body: { ...base(), ...extra } });

  async function load() {
    data = await P(`subtitles/list?file=${enc(meta.file)}&title=${enc(meta.title || "")}`);
    paint();
    const job = data.state?.job;
    if (job?.phase === "running" || data.busy) schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (closed) return;
      try { await load(); } catch { /* keep trying */ }
    }, 2500);
  }

  function paint() {
    fill(body, jobView(), listView(), adaptView(), searchView(), translateView());
  }

  // ---- background job banner ----
  function jobView() {
    const j = data.state?.job;
    if (!j || !j.kind || (j.phase !== "running" && Date.now() / 1000 - (j.at || 0) > 600)) return null;
    const KIND = { pick: t("Download and sync"), adapt: t("Sync again"), translate: t("Translation") };
    if (j.phase === "running") {
      const pct = j.total ? ` ${Math.round(100 * j.done / j.total)}%` : "";
      return h("div.m-banner", h("div.spin"), h("div", h("b", KIND[j.kind] || j.kind), pct,
        j.total ? h("div.progress", { style: { marginTop: "6px" } }, h("div", { style: { width: `${100 * j.done / j.total}%` } })) : null));
    }
    const ok = j.ok !== false && j.outcome !== "rejected" && j.outcome !== "unverified";
    return h(`div.m-banner${ok ? ".m-ok" : ""}`, ok ? "✅" : "⚠️", h("div.stack", { style: { gap: "4px" } },
      h("b", outcomeText(j)),
      j.message && h("span.small", j.message),
      j.usd ? h("span.small.muted", t("Cost about {usd} $", { usd: Number(j.usd).toFixed(3) })) : null,
      j.can_force && h("div.row", h("button.small", { onclick: (e) => act(() => forcePick(), e.currentTarget) }, t("Use it anyway")))));
  }
  let lastPick = null;
  const outcomeText = (j) => ({
    in_sync: t("In sync with the video"), adapted: j.offset != null ? t("Adapted: shifted by {s} s", { s: (-j.offset).toFixed(1) }) : t("Adapted"),
    translated: t("Translated"), rejected: t("Rejected"), unverified: t("Saved, timing not verified"),
  }[j.outcome] || (j.ok === false ? t("Failed") : t("Done")));

  // ---- what exists ----
  function listView() {
    const subs = data.subs || [], emb = data.embedded || [];
    const names = LANGNAME();
    const row = (x) => h("div.item.m-sub",
      h("span", { style: { fontSize: "20px" } }, flag(x.lang)),
      h("div.stack", { style: { gap: "2px", minWidth: 0, flex: "1 1 170px" } },
        h("b", { style: { overflowWrap: "anywhere" } }, x.kind === "embedded" ? `${names[x.lang] || x.lang || t("unknown language")} · ${t("inside the video")}` : x.name),
        h("div.row.wrap", { style: { gap: "6px" } },
          x.kind === "sidecar" && h("span.small.muted", fmt.bytes(x.size)),
          x.forced && h("span.chip", t("forced")), x.hi && h("span.chip", t("hearing impaired")),
          x.origin === "translated" && h("span.chip.warn", t("Translated ({name})", { name: data.state?.translated?.provider === "claude" ? "Claude" : "DeepL" })),
          x.origin && x.origin !== "translated" && ORIGIN()[x.origin] && h("span.chip.ok", ORIGIN()[x.origin]))),
      x.kind === "sidecar" && h("div.row.wrap.m-subact",
        x.editable && h("button.small", { onclick: () => { adapting = adapting === x.name ? null : x.name; adaptState = null; paint(); } }, t("Adapt")),
        x.origin === "translated" && h("button.small", { onclick: () => doSearch(["it"]) }, t("Find a real Italian one"))));
    return h("div.stack", { style: { gap: "6px" } },
      h("h3", { style: { margin: 0 } }, t("On this video")),
      subs.length + emb.length ? h("div.list", [...subs, ...emb].map(row)) : h("div.small.muted", t("No subtitles next to the video.")));
  }

  // ---- adapter (auto + manual offset / fps) ----
  let adaptState = null;  // {offset, scale, preset, rows}
  function adaptView() {
    if (!adapting) return null;
    const x = (data.subs || []).find((y) => y.name === adapting);
    if (!x) return null;
    const st = adaptState ||= { offset: 0, scale: 1, preset: "", rows: null };
    const val = h("input", { type: "number", step: "0.1", value: st.offset.toFixed(1), style: { width: "92px", textAlign: "center" },
      "aria-label": t("Offset in seconds"), onchange: (e) => setOffset(parseFloat(e.target.value) || 0) });
    const step = (d) => h("button.small", { onclick: () => setOffset(Math.round((st.offset + d) * 10) / 10) }, (d > 0 ? "+" : "−") + Math.abs(d));
    const presets = { "": t("no frame-rate change"), "23.976->25": "23.976 → 25", "25->23.976": "25 → 23.976", "24->25": "24 → 25", "25->24": "25 → 24" };
    const SCALE = { "23.976->25": 23.976 / 25, "25->23.976": 25 / 23.976, "24->25": 24 / 25, "25->24": 25 / 24 };
    const sel = h("select", { "aria-label": t("Frame rate"), onchange: (e) => { st.preset = e.target.value; st.scale = SCALE[st.preset] || 1; preview(); } },
      Object.entries(presets).map(([k, l]) => h("option", { value: k, selected: k === st.preset }, l)));
    const rows = h("div.m-prev");
    const drawRows = () => fill(rows, st.rows ? [
      h("div.small.muted", t("{n} cues. A few of them, before → after:", { n: st.cues })),
      ...st.rows.map((r) => h("div.m-prow", h("span.small.muted", `#${r.n}`), h("span.small", `${clock(r.old)} → `, h("b", clock(r.new))),
        h("span.small.muted.ellipsis", r.text)))] : h("div.small.muted", t("Move the offset to see the new times.")));
    const setOffset = (v) => { st.offset = v; val.value = v.toFixed(1); preview(); };
    let tmr;
    const preview = () => { clearTimeout(tmr); tmr = setTimeout(async () => {
      try {
        const r = await call("adapt", { mode: "manual", name: x.name, offset: st.offset, scale: st.scale });
        st.rows = r.rows; st.cues = r.cues; drawRows();
      } catch (e) { toast(e.message, true); }
    }, 250); };
    drawRows();
    if (!st.rows) preview();
    return h("div.stack.m-adapt", { style: { gap: "10px" } },
      h("div.row", h("h3", { style: { margin: 0 } }, t("Adapt")), h("span.small.muted.ellipsis", x.name),
        h("div.spacer"), h("button.icon.ghost", { "aria-label": t("Close"), onclick: () => { adapting = null; paint(); } }, icon("x"))),
      h("div.row.wrap", h("button", { onclick: (e) => act(async () => { await call("adapt", { mode: "auto", name: x.name }); toast(t("Syncing…")); await load(); }, e.currentTarget) },
        icon("refresh"), t("Sync again automatically")),
        h("span.small.muted", t("Compares with the other subtitles or the audio."))),
      h("div.stack", { style: { gap: "6px" } },
        h("b.small", t("Manual offset (positive = later)")),
        h("div.row.wrap", { style: { gap: "6px" } }, step(-1), step(-0.1), val, step(0.1), step(1), h("span.small.muted", "s"))),
      h("div.row.wrap", h("label.small", t("Frame rate")), sel),
      rows,
      h("div.row.wrap", { style: { justifyContent: "flex-end" } },
        x.has_backup && h("button.ghost", { onclick: (e) => act(async () => { await call("adapt", { mode: "restore", name: x.name }); toast(t("Original restored")); adaptState = null; await load(); }, e.currentTarget) },
          t("Restore the original")),
        h("button.primary", { onclick: (e) => act(async () => {
          await call("adapt", { mode: "manual", name: x.name, offset: st.offset, scale: st.scale, save: true });
          toast(t("Saved")); adaptState = null; await load();
        }, e.currentTarget) }, t("Save"))));
  }
  const clock = (sec) => { const m = Math.floor(sec / 60), r = sec - m * 60; return `${String(m).padStart(2, "0")}:${r.toFixed(1).padStart(4, "0")}`; };

  // ---- online search ----
  async function doSearch(langs) {
    results = { loading: true };
    paint();
    try {
      results = await call("search", { langs });
      resLang = langs[0] || "it";
    } catch (e) { results = { error: e.message }; }
    paint();
  }
  async function forcePick() { if (lastPick) await pick(lastPick, true); }
  async function pick(c, force = false) {
    lastPick = c;
    await call("pick", { provider: c.provider, id: c.id, lang: c.lang, release: c.release, hash_match: c.hash_match, force });
    toast(t("Downloading and syncing…"));
    await load();
  }
  function searchView() {
    const configured = (data.providers || []).length > 0;
    const head = h("div.row.wrap", h("h3", { style: { margin: 0, flex: 1 } }, t("Search online")),
      h("button.primary", { disabled: !configured, onclick: (e) => act(() => doSearch(["it", "en"]), e.currentTarget) }, icon("search"), t("Search Italian and English")));
    if (!configured) return h("div.stack", head, h("div.small.muted", t("Add an OpenSubtitles or SubDL key in Settings → Media.")));
    if (!results) return h("div.stack", head,
      h("div.small.muted", t("From {providers}.", { providers: data.providers.map((p) => p === "opensubtitles" ? "OpenSubtitles" : "SubDL").join(", ") })),
      !data.os_quota_left && h("div.small", { style: { color: "var(--warn)" } }, t("OpenSubtitles daily download limit reached; it resets tomorrow.")));
    if (results.loading) return h("div.stack", head, loading());
    if (results.error) return h("div.stack", head, errorCard(results.error, { title: t("Search failed") }));
    const all = results.candidates || [];
    const seg = h("div.seg", ["it", "en"].map((l) => h(`button${l === resLang ? ".active" : ""}`, { onclick: () => { resLang = l; paint(); } },
      `${flag(l)} ${LANGNAME()[l]} (${all.filter((c) => c.lang === l).length})`)));
    const mine = all.filter((c) => c.lang === resLang);
    return h("div.stack", { style: { gap: "8px" } }, head, seg,
      mine.length ? h("div.list", mine.map(candRow)) : h("div.small.muted", t("Nothing found.")));
  }
  function candRow(c) {
    return h("div.item.m-cand",
      h("div.stack", { style: { gap: "3px", minWidth: 0, flex: 1 } },
        h("b.ellipsis", { title: c.release }, c.release || c.name || c.id),
        h("div.row.wrap", { style: { gap: "6px" } },
          h("span.chip.accent", c.provider === "opensubtitles" ? "OpenSubtitles" : "SubDL"),
          h(`span.chip${c.hash_match ? ".ok" : c.match >= 60 ? ".ok" : c.match >= 30 ? "" : ".warn"}`,
            c.hash_match ? t("exact file match") : t("{n}% similar", { n: c.match })),
          c.downloads ? h("span.small.muted", `⬇ ${num(c.downloads)}`) : null,
          c.hi && h("span.chip", t("hearing impaired")), c.forced && h("span.chip.warn", t("forced only")))),
      h("button.small", { onclick: (e) => act(() => pick(c), e.currentTarget) }, icon("download"), t("Download and sync")));
  }

  // ---- translation ----
  let est = null, estPending = false, tProvider = "";
  function translateView() {
    const names = (data.translators || []);
    const hasIt = (data.subs || []).some((x) => x.lang === data.language && !x.forced) && data.state?.translated == null;
    if (!names.length) return h("div.stack", { style: { gap: "4px" } }, h("h3", { style: { margin: 0 } }, t("Translate to Italian")),
      h("div.small.muted", t("Add a DeepL or Anthropic key in Settings → Media to translate an English subtitle.")));
    if (hasIt) return null;
    const provider = tProvider || names[0].name;
    const sel = names.length > 1 ? h("select", { "aria-label": t("Translator"), onchange: (e) => { tProvider = e.target.value; est = null; paint(); } },
      names.map((n) => h("option", { value: n.name, selected: n.name === provider }, n.label))) : h("b", names[0].label);
    const info = h("div.small.muted");
    const showEst = () => {
      if (!est) { info.textContent = ""; return; }
      if (est.error) { info.textContent = est.error; return; }
      const parts = [t("{n} cues from {lang}", { n: est.cues, lang: LANGNAME()[est.lang] || est.lang }), t("{n} characters", { n: num(est.chars) })];
      if (provider === "deepl" && est.deepl) parts.push(t("DeepL: {left} left this month", { left: num(est.deepl.remaining) }) + (est.deepl.enough ? "" : ` · ${t("not enough")}`));
      if (provider === "claude" && est.claude) parts.push(t("about {usd} $ with {model}", { usd: est.claude.usd.toFixed(2), model: est.claude.model }));
      info.textContent = parts.join(" · ");
    };
    if (!est && !estPending) {
      estPending = true;
      call("translate", { dry: true }).then((r) => { est = r; }).catch((e) => { est = { error: e.message }; })
        .finally(() => { estPending = false; showEst(); });
    }
    showEst();
    return h("div.stack", { style: { gap: "8px" } },
      h("h3", { style: { margin: 0 } }, t("Translate to Italian")),
      h("div.row.wrap", { style: { gap: "8px" } }, t("with"), sel,
        h("button", { onclick: (e) => act(async () => { await call("translate", { provider }); toast(t("Translating…")); await load(); }, e.currentTarget) },
          t("Translate"))),
      info,
      h("div.small.muted", t("Only the subtitle text and the title are sent. The file is marked as a translation and replaced when a real Italian one turns up.")));
  }

  const start = () => load().catch((e) => fill(body, errorCard(e.message, { title: t("Couldn't load the subtitles"), retry: start })));
  start();
  return s;
}

/** The "Sottotitoli: …" card on the Library tab: counts, today's budget, pause / resume. */
export function scanCard() {
  const box = h("div.m-scan");
  let timer;
  async function paint() {
    if (!box.isConnected && timer) { clearInterval(timer); timer = null; return; }
    let s;
    try { s = await P("subtitles/scan"); } catch { return; }
    if (!s.enabled) { fill(box); return; }
    const c = s.counts || {};
    const REASON = {
      paused: t("paused"), "daily limit reached": t("daily limit reached"), "CPU busy": t("waiting: the hub is busy"),
      "OpenSubtitles quota used up": t("waiting: OpenSubtitles limit reached"), "another subtitle job is running": t("waiting: another job is running"),
      "nothing left to do": t("nothing left to do"), "no provider": t("add an OpenSubtitles key in Settings"),
    };
    const parts = [
      t("{n} to search", { n: c.todo ?? 0 }), t("{n} found", { n: c.found ?? 0 }),
      c.translated ? t("{n} translated", { n: c.translated }) : null,
      c.not_found ? t("{n} not found", { n: c.not_found }) : null,
    ].filter(Boolean);
    fill(box, h("div.row.wrap", { style: { gap: "10px" } },
      h("div.stack", { style: { gap: "2px", flex: 1, minWidth: "200px" } },
        h("div", h("b", t("Subtitles")), ": ", parts.join(" · ")),
        h("div.small.muted", s.current ? t("Working on {name}", { name: s.current })
          : `${t("Today: {n} of {max}", { n: s.done_today, max: s.per_day || "∞" })}${s.reason ? ` · ${REASON[s.reason] || s.reason}` : ""}`)),
      h("button.small", { onclick: (e) => act(async () => { await P("subtitles/scan", { body: { paused: !s.paused } }); await paint(); }, e.currentTarget) },
        icon(s.paused ? "play" : "pause"), s.paused ? t("Resume") : t("Pause")),
      !s.paused && h("button.small.ghost", { onclick: (e) => act(async () => { await P("subtitles/scan", { body: { run: true } }); toast(t("Started")); }, e.currentTarget) }, t("Run now"))));
  }
  paint();
  timer = setInterval(paint, 15000);
  return box;
}
