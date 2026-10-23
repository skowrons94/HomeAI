// Torrent search: one box over the providers enabled in Settings (hub/plugins/torrents/).
// Route: #/torrents. Results are normalised by the hub; filters and sorting run server-side.
import { api, h, icon, toast, ensureCss, fmt, t, sheet } from "../app.js";

const state = { q: "", sort: "relevance", order: "desc", minSeeders: 0, maxGb: 0, page: 1, results: [], ids: new Set() };
let seq = 0;          // ignore answers of superseded searches
let els = {};

const qs = () => {
  const p = new URLSearchParams({ q: state.q, sort: state.sort, order: state.order, page: state.page });
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
      r.magnet ? h("button", { onclick: () => copy(r.magnet) }, t("Copy magnet")) : null,
      r.magnet ? h("a.button", { href: r.magnet }, t("Open magnet")) : null,
      r.url ? h("a.button", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, t("Torrent file")) : null));
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
  if (error) body.push(h("div.tr-error", error));
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

const option = (value, label, cur) => h("option", { value, selected: value === cur }, label);

export default {
  async render(root) {
    ensureCss(`/static/pages/torrents.css?v=${window.CASA_V}`);
    const input = h("input", { type: "search", placeholder: t("Search…"), value: state.q, maxlength: 100,
                               enterkeyhint: "search", oninput: (e) => { state.q = e.target.value; } });
    els = { go: h("button.primary", { type: "submit" }, icon("search"), t("Search")),
            status: h("div.tr-status"), searched: h("div.row.wrap.tr-searched"), list: h("div.tr-list") };
    const again = () => state.q && run();
    root.replaceChildren(
      h("div.row", h("h1", t("Torrents")), h("div.spacer"),
        h("button", { onclick: trackerMenu }, icon("settings"), t("Trackers"))),
      h("form.tr-form", { onsubmit: (e) => { e.preventDefault(); run(); } }, input, els.go),
      h("div.tr-filters",
        h("label", t("Sort"), h("select", { onchange: (e) => { state.sort = e.target.value; again(); } },
          option("relevance", t("Relevance"), state.sort), option("seeders", t("Seeders"), state.sort),
          option("size", t("Size"), state.sort))),
        h("label", t("Order"), h("select", { onchange: (e) => { state.order = e.target.value; again(); } },
          option("desc", t("Descending"), state.order), option("asc", t("Ascending"), state.order))),
        h("label", t("Min seeders"), h("input", { type: "number", min: 0, value: state.minSeeders,
          onchange: (e) => { state.minSeeders = Math.max(0, +e.target.value || 0); again(); } })),
        h("label", t("Max size (GB)"), h("input", { type: "number", min: 0, step: "0.5", value: state.maxGb,
          onchange: (e) => { state.maxGb = Math.max(0, +e.target.value || 0); again(); } }))),
      els.searched, els.status, els.list);
    draw();
    loadEnabled();
  },
};
