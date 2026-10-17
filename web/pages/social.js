// Shared feed: friends' diary (watched / rated / watchlist) and shared recipes with tips (hub/plugins/sync).
// Routes: #/social[/diary|recipes|recipe/<slug>]
import { api, h, icon, toast, ensureCss, act, debounce, fmt, go, hasPlugin, t, locale } from "../app.js";

const P = (path, opts) => api(`p/sync/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const loading = () => h("div.empty", h("div.spin"));
const TABS = [["diary", "Diary"], ["recipes", "Recipes"]];
const KINDS = [["", "All"], ["watch", "Watched"], ["rating", "Ratings"], ["watchlist", "Watchlist"]];
const PAGE = 40;

const toSec = (ts) => (typeof ts === "number" ? ts : Date.parse(ts) / 1000);
const imgUrl = (n) => !n ? null : /^(\/|https?:)/.test(n) ? n : `/api/p/sync/image/${enc(n)}`;
const stars = (r) => {
  const full = Math.floor(r), half = r - full >= 0.5;
  return "★".repeat(full) + (half ? "½" : "");
};
const day = (d) => new Date(d + "T12:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });

function syncLine(st) {
  const bits = [];
  if (st.last_sync) bits.push(t("Last sync {when}", { when: fmt.ago(toSec(st.last_sync)) }));
  else bits.push(t("Never synced"));
  if (st.pending) bits.push(t("{n} pending", { n: st.pending }));
  return h("div.tiny.muted.s-sync", bits.join(" · "),
    st.last_error && h("span.s-err", ` · ${st.last_error}`));
}

export default {
  async render(root, args) {
    ensureCss(`/static/pages/social.css?v=${window.CASA_V}`);
    const [view, slug] = args;
    if (!hasPlugin("sync")) { fill(root, h("div.empty", t("Shared feed is off: set it up in Settings."))); return; }
    let st;
    try { st = await P("status"); } catch (e) { fill(root, h("div.empty", e.message)); return; }
    if (!st.configured) {
      fill(root, h("div.empty", t("Shared feed is off: set it up in Settings."),
        h("div", { style: { marginTop: "12px" } }, h("button.primary", { onclick: () => go("#/settings") }, icon("settings"), t("Open Settings")))));
      return;
    }
    if (view === "recipe" && slug) { await renderRecipe(root, slug); return; }

    const tab = view === "recipes" ? "recipes" : "diary";
    const body = h("div.s-body");
    const line = h("div");
    const refreshLine = (s) => fill(line, syncLine(s));
    refreshLine(st);
    const syncBtn = h("button", { onclick: (e) => act(async () => {
      st = await P("sync", { body: {} });
      refreshLine(st);
      toast(st.last_error ? t("Sync finished with an error") : t("Synced"), !!st.last_error);
      if (tab === "diary") renderDiary(body, st);
    }, e.currentTarget) }, icon("refresh"), t("Sync now"));
    fill(root,
      h("div.s-head", h("h1", t("Feed")), syncBtn),
      line,
      h("div.tabs", { style: { margin: "10px 0" } }, TABS.map(([k, l]) =>
        h(`button${k === tab ? ".active" : ""}`, { onclick: () => go(`#/social/${k}`) }, t(l)))),
      body);
    if (tab === "recipes") await renderRecipes(body);
    else await renderDiary(body, st);
  },
};

// ---------------------------------------------------------------- diary

function what(it) {
  if (it.kind === "watch") return it.watched_at ? t("watched on {date}", { date: day(it.watched_at) }) : null;
  if (it.kind === "rating") return it.rating != null ? h("span", h("span.s-stars", stars(it.rating)), ` ${it.rating}`) : null;
  if (it.kind === "watchlist") return it.on ? t("added to watchlist") : t("removed from watchlist");
  return it.kind;
}

function diaryCard(it) {
  const w = what(it);
  if (w === null) return null;  // undone watch / removed rating
  const type = it.media === "series" ? "series" : "movie";
  return h("div.s-card", { onclick: () => go(`#/media/${type}/${enc(it.imdb_id)}`) },
    it.poster ? h("img", { src: it.poster, loading: "lazy", alt: "" }) : h("div.ph", icon("film")),
    h("div.s-info",
      h("div.s-title", it.title || it.imdb_id, it.year && h("span.muted", ` (${it.year})`)),
      h("div.row.wrap.small", { style: { gap: "6px" } }, h("span.chip.accent", it.user), h("span", w)),
      it.note && h("div.s-note", `“${it.note}”`),
      h("div.tiny.muted", fmt.ago(toSec(it.ts)))));
}

async function renderDiary(el, st) {
  const state = { user: "", kind: "" };
  const chips = h("div.s-filters"), list = h("div.s-list"), more = h("div.s-more");
  fill(el, chips, list, more);
  let last = null;

  const paintChips = () => fill(chips,
    (st.users || []).length > 1 && (st.users || []).map((u) =>
      h(`button.chipbtn${state.user === u ? ".active" : ""}`, { onclick: () => { state.user = state.user === u ? "" : u; load(true); } }, u)),
    KINDS.map(([k, l]) => h(`button.chipbtn${state.kind === k ? ".active" : ""}`, { onclick: () => { state.kind = k; load(true); } }, t(l))));

  async function load(reset) {
    if (reset) { last = null; fill(list, loading()); paintChips(); }
    const q = new URLSearchParams({ limit: PAGE });
    if (state.user) q.set("user", state.user);
    if (state.kind) q.set("kind", state.kind);
    if (last) q.set("before", last);
    let items;
    try { items = await P(`feed?${q}`); } catch (e) { fill(list, h("div.empty", e.message)); fill(more); return; }
    if (reset) fill(list);
    const cards = items.map(diaryCard).filter(Boolean);
    list.append(...cards);
    if (items.length) last = items[items.length - 1].ts;
    if (!list.children.length) fill(list, h("div.empty", t("Nothing here yet. Rate or mark something as watched in Media.")));
    fill(more, items.length >= PAGE && h("button", { onclick: (e) => act(() => load(false), e.currentTarget) }, t("Load more")));
  }
  await load(true);
}

// ---------------------------------------------------------------- recipes

async function renderRecipes(el) {
  const state = { q: "", tag: "" };
  const q = h("input", { type: "search", placeholder: t("Search shared recipes…") });
  const filters = h("div.s-filters"), grid = h("div.s-rgrid");
  fill(el, h("div.s-search", h("div.s-input", icon("search"), q)), filters, grid);
  let allTags = [];

  async function load() {
    let items;
    try { items = await P(`recipes?q=${enc(state.q)}&tag=${enc(state.tag)}`); }
    catch (e) { fill(grid, h("div.empty", e.message)); return; }
    if (!state.q && !state.tag) allTags = [...new Set(items.flatMap((r) => r.tags || []))].sort();
    fill(filters, allTags.slice(0, 16).map((x) =>
      h(`button.chipbtn${state.tag === x ? ".active" : ""}`, { onclick: () => { state.tag = state.tag === x ? "" : x; load(); } }, x)));
    fill(grid, items.length ? items.map((r) =>
      h("div.s-rcard", { onclick: () => go(`#/social/recipe/${enc(r.slug)}`), title: r.title },
        h("div.img", r.image ? h("img", { src: imgUrl(r.image), loading: "lazy", alt: "" }) : h("div.ph", icon("utensils")),
          r.tips_count ? h("span.s-badge", t("{n} tips", { n: r.tips_count })) : null),
        h("div.t", r.title),
        h("div.y", [r.author, r.time_min && t("{n} min", { n: r.time_min })].filter(Boolean).join(" · "))))
      : h("div.empty", state.q || state.tag ? t("Nothing found.") : t("No shared recipes yet. Open one of your recipes and tap Share with friends.")));
  }
  q.addEventListener("input", debounce(() => { state.q = q.value.trim(); load(); }, 250));
  await load();
}

async function renderRecipe(root, slug) {
  fill(root, loading());
  let r;
  try { r = await P(`recipes/${enc(slug)}`); } catch (e) { fill(root, h("div.empty", e.message)); return; }
  const paint = () => {
    const tipInput = h("input", { placeholder: t("Add your tip…"), enterkeyhint: "send", maxlength: 500 });
    const addTip = (e) => { e?.preventDefault?.(); const text = tipInput.value.trim(); if (!text) return;
      act(async () => { r = await P(`recipes/${enc(slug)}/tips`, { body: { text } }); toast(t("Tip added")); paint(); }); };
    let n = 0;
    const meta = [r.author && t("by {author}", { author: r.author }), r.servings && t("{n} servings", { n: r.servings }), r.time_min && t("{n} min", { n: r.time_min })].filter(Boolean).join(" · ");
    fill(root, h("div.s-detail",
      h("div.s-hero", r.image ? h("img", { src: imgUrl(r.image), alt: "" }) : h("div.ph", icon("utensils")),
        h("button.icon.back", { onclick: () => go("#/social/recipes"), "aria-label": t("Back") }, icon("left"))),
      h("h1", r.title),
      h("div.row.wrap.small.muted", { style: { gap: "8px" } }, meta,
        (r.tags || []).map((x) => h("span.chip", x)),
        r.source && /^https?:/.test(r.source) && h("a", { href: r.source, target: "_blank", rel: "noopener" }, icon("link"), " ", t("source"))),
      h("div.row.wrap", h("button.primary", { onclick: (e) => act(async () => {
        const res = await P(`recipes/${enc(slug)}/import`, { body: {} });
        toast(t("Added to your recipes"));
        if (res?.id != null) go(`#/recipes/recipe/${res.id}`);
      }, e.currentTarget) }, icon("plus"), t("Add to my recipes"))),
      h("div.s-cols",
        h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Ingredients")),
          h("div.s-ings", (r.ingredients || []).map((i) => typeof i === "string"
            ? (i.startsWith("# ") ? h("div.s-sub", i.slice(2)) : h("div.s-ing", i)) : h("div.s-ing", i.text || i.heading)))),
        h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Method")),
          (r.steps || []).map((s) => s.startsWith("# ") ? h("div.s-sub", s.slice(2))
            : h("div.s-step", h("span.n", ++n), h("p", s))))),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Tips")),
        (r.tips || []).length ? (r.tips || []).map((tp) => h("div.s-tip", h("span.chip.accent", tp.user), h("span", tp.text)))
          : h("div.muted.small", t("No tips yet.")),
        h("form.s-search", { onsubmit: addTip, style: { margin: 0 } }, tipInput,
          h("button.primary", { type: "submit" }, icon("plus"), t("Add"))))));
  };
  paint();
}
