// Torrent search: one box over the providers enabled in Settings (hub/plugins/torrents/).
// Route: #/torrents. Results are normalised by the hub; filters and sorting run server-side.
import { api, h, icon, toast, ensureCss, fmt, t } from "../app.js";

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
            status: h("div.tr-status"), list: h("div.tr-list") };
    const again = () => state.q && run();
    root.replaceChildren(
      h("h1", t("Torrents")),
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
      els.status, els.list);
    draw();
  },
};
