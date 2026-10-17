// Recipes: discover/import, personal recipe book, shopping list, calorie log (hub/plugins/recipes).
// Routes: #/recipes[/discover|mine|shopping|today|recipe/<id>]
import { api, h, icon, toast, sheet, ensureCss, act, debounce, go, hasPlugin, t, locale } from "../app.js";

const P = (path, opts) => api(`p/recipes/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const loading = () => h("div.empty", h("div.spin"));

const fmtMin = (m) => !m ? "" : m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60}` : ""}` : `${m} ${t("min")}`;
const kcalText = (r) => r.kcal ? `${r.kcal_est ? "~" : ""}${r.kcal} kcal` : "";
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const isUrl = (s) => /^https?:\/\/\S+$/i.test(s.trim());
const FLAG = { it: "🇮🇹", en: "🇬🇧" };

let status = { language: "it", goal: 2000 };

const TABS = ["discover", "mine", "shopping", "today"];
const TAB_LABEL = () => ({ discover: t("Discover"), mine: t("My recipes"), shopping: t("Shopping"), today: t("Today") });

export default {
  async render(root, args) {
    ensureCss(`/static/pages/recipes.css?v=${window.CASA_V}`);
    try { status = await P("status"); } catch (e) { fill(root, h("div.empty", e.message)); return; }
    const [view, id] = args;
    if (view === "recipe") { await renderDetail(root, id); return; }

    const tab = TABS.includes(view) ? view : "discover";
    const body = h("div.r-body");
    fill(root,
      h("div.r-head", h("h1", t("Recipes")),
        h("button.primary", { onclick: () => newRecipe() }, icon("plus"), t("New recipe"))),
      h("div.tabs", TABS.map((k) =>
        h(`button${k === tab ? ".active" : ""}`, { onclick: () => go(`#/recipes/${k}`) }, TAB_LABEL()[k]))),
      body);
    ({ discover: renderDiscover, mine: renderMine, shopping: renderShopping, today: renderToday })[tab](body);
  },
};

// ---------------------------------------------------------------- cards

function card(r, onclick, extra) {
  const img = r.image ? h("img", { src: r.image, loading: "lazy", alt: "" }) : h("div.ph", icon("utensils"));
  return h("div.r-card", { onclick, title: r.title },
    h("div.img", img, extra),
    h("div.t", r.title),
    h("div.y", r.meta));
}

// ---------------------------------------------------------------- discover

function renderDiscover(el) {
  const input = h("input", { type: "search", placeholder: t("Search or paste a link…"), enterkeyhint: "search",
                             value: sessionStorage.getItem("r-q") || "" });
  const out = h("div");
  const btn = h("button.primary", { type: "submit" });
  const label = () => fill(btn, icon(isUrl(input.value) ? "download" : "search"), isUrl(input.value) ? t("Import") : t("Search"));
  label();
  input.addEventListener("input", label);
  const submit = async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    try { sessionStorage.setItem("r-q", q); } catch {}
    if (isUrl(q)) return importRecipe({ url: q }, btn);
    fill(out, loading());
    try { showResults(out, await P(`search?q=${enc(q)}`), q); }
    catch (err) { fill(out, h("div.empty", err.message)); }
  };
  fill(el, h("form.r-search", { onsubmit: submit }, h("div.r-input", icon("search"), input), btn), out);
  if (input.value.trim() && !isUrl(input.value)) submit(new Event("submit"));
  else fill(out, h("div.empty", t("Search GialloZafferano and TheMealDB, or paste the link of a recipe from any site: it is saved on your hub and works offline.")));
}

function showResults(out, res, q) {
  const warn = Object.keys(res.errors || {}).length
    ? h("div.r-banner", "⚠️", h("div", `${t("Some sources didn't answer")}: ${Object.entries(res.errors).map(([k, v]) => `${k} (${v})`).join(", ")}`)) : null;
  fill(out, warn, res.results.length
    ? h("div.r-grid", res.results.map((r) => {
      const c = card({ image: r.image, title: r.title, meta: `${FLAG[r.lang] || ""} ${r.subtitle || r.site}` },
        () => importRecipe(r.mealdb ? { mealdb: r.mealdb } : { url: r.url }, c),
        r.saved ? h("span.r-badge", icon("check")) : null);
      return c;
    }))
    : h("div.empty", `${t("Nothing found.")} “${q}”`));
}

async function importRecipe(body, busyEl) {
  busyEl.classList.add("busy");
  if (busyEl.tagName === "BUTTON") busyEl.disabled = true;
  try {
    toast(t("Importing…"));
    const r = await P("import", { body });
    if (!r.existing) toast(t("Recipe saved"));
    go(`#/recipes/recipe/${r.id}`);
  } catch (e) { toast(e.message, true); }
  finally { busyEl.classList.remove("busy"); if (busyEl.tagName === "BUTTON") busyEl.disabled = false; }
}

async function newRecipe() {
  const title = h("input", { placeholder: t("Title") });
  const ing = h("textarea", { rows: 6, placeholder: t("200 g spaghetti\n2 eggs") });
  const steps = h("textarea", { rows: 4 });
  const sv = h("input", { type: "number", min: 1, value: status.default_servings || 2 });
  const s = sheet(t("New recipe"), [
    h("label.field", t("Title"), title), h("label.field", t("Servings"), sv),
    h("label.field", t("Ingredients (one per line, '# Heading' for a group)"), ing), h("label.field", t("Method (one step per line)"), steps),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      if (!title.value.trim()) return toast(t("A title is required"), true);
      const r = await P("recipes", { body: { title: title.value, servings: +sv.value, ingredients: ing.value, steps: steps.value } });
      s.close(); go(`#/recipes/recipe/${r.id}`);
    }, e.currentTarget) }, t("Save"))]);
  title.focus();
}

// ---------------------------------------------------------------- my recipes

async function renderMine(el) {
  const state = { fav: false, tag: "", q: "", edit: false };
  const list = h("div"), filters = h("div.r-filters");
  const q = h("input", { type: "search", placeholder: t("Filter your recipes…") });
  const editBtn = h("button.r-editbtn", { onclick: () => { state.edit = !state.edit; refresh(); } });
  fill(el, h("div.r-search", h("div.r-input", icon("search"), q), editBtn), filters, list);
  const tags = await P("tags");
  const paintFilters = () => fill(filters,
    h(`button.chipbtn${state.fav ? ".active" : ""}`, { onclick: () => { state.fav = !state.fav; refresh(); } }, icon("heart"), t("Favourites")),
    tags.slice(0, 14).map((x) => h(`button.chipbtn${state.tag === x.tag ? ".active" : ""}`,
      { onclick: () => { state.tag = state.tag === x.tag ? "" : x.tag; refresh(); } }, x.tag)));
  async function refresh() {
    paintFilters();
    fill(editBtn, icon(state.edit ? "check" : "edit"), state.edit ? t("Done editing") : t("Edit"));
    editBtn.classList.toggle("active", state.edit);
    const items = await P(`recipes?fav=${state.fav ? 1 : 0}&tag=${enc(state.tag)}&q=${enc(state.q)}`);
    fill(list, items.length
      ? h(`div.r-grid${state.edit ? ".editing" : ""}`, items.map((r) => {
        const c = card({ ...r, meta: [kcalText(r), fmtMin(r.total_min)].filter(Boolean).join(" · ") || r.site },
          () => { if (!state.edit) go(`#/recipes/recipe/${r.id}`); },
          [r.fav && h("span.r-badge.fav", icon("heart")), r.edited && h("span.r-badge.ed", icon("edit")),
            state.edit && h("button.danger.r-cardel", { "aria-label": t("Delete"), onclick: (e) => {
              e.stopPropagation();
              if (!confirm(t("Delete this recipe from your hub?") + "\n\n" + r.title)) return;
              act(async () => {
                await P(`recipe/${r.id}`, { method: "DELETE" });
                c.remove(); toast(t("Recipe deleted"));
                if (!list.querySelector(".r-card")) refresh();
              }, e.currentTarget);
            } }, icon("trash"), t("Delete"))]);
        return c;
      }))
      : h("div.empty", state.fav || state.tag || state.q ? t("Nothing found.") : t("No saved recipes yet. Search or import your first one!")));
  }
  q.addEventListener("input", debounce(() => { state.q = q.value; refresh(); }, 250));
  await refresh();
}

// ---------------------------------------------------------------- detail

async function renderDetail(root, id) {
  fill(root, loading());
  let rec;
  try { rec = await P(`recipe/${id}`); } catch (e) { fill(root, h("div.empty", e.message)); return; }
  const view = h("div.r-detail");
  fill(root, view);
  let servings = rec.servings;
  const reload = async (patch) => {
    if (patch) await P(`recipe/${id}`, { method: "PUT", body: patch });
    rec = await P(servings ? `recipe/${id}?servings=${servings}` : `recipe/${id}`);
    servings = rec.servings;
  };
  const paint = () => paintDetail(view, rec, { reload, paint, id, get servings() { return servings; }, set servings(v) { servings = v; } });
  paint();
}

function paintDetail(view, rec, ctx) {
  const { id } = ctx;
  const upd = (patch) => act(async () => { await ctx.reload(patch); ctx.paint(); });

  const heart = h(`button.icon.heart${rec.fav ? ".on" : ""}`, { title: t("Favourites"), onclick: () => upd({ fav: !rec.fav }) }, icon("heart"));
  const stars = h("div.stars", [1, 2, 3, 4, 5].map((n) =>
    h(`button.star${n <= rec.rating ? ".on" : ""}`, { onclick: () => upd({ rating: rec.rating === n ? 0 : n }), "aria-label": `${n}` }, icon("star"))));

  const meta = [
    rec.prep_min && `${t("prep")} ${fmtMin(rec.prep_min)}`, rec.cook_min && `${t("cook")} ${fmtMin(rec.cook_min)}`,
    !rec.prep_min && !rec.cook_min && rec.total_min && fmtMin(rec.total_min),
  ].filter(Boolean).join(" · ");

  const kcalChip = rec.kcal
    ? h("span.chip.accent", { title: rec.kcal_est ? t("estimated from the ingredients") : "" }, `${rec.kcal_est ? "~" : ""}${rec.kcal} ${t("kcal per serving")}`)
    : h("span.chip", t("calories unknown"));

  let target = rec.servings;
  const val = h("span.val", target % 1 ? target.toFixed(1) : target);
  const apply = debounce(() => { ctx.servings = target; act(async () => { await ctx.reload(); ctx.paint(); }); }, 350);
  const step = (d) => { target = Math.max(1, Math.round(target) + d); val.textContent = target; apply(); };
  const sv = h("div.stepper",
    h("button.icon", { onclick: () => step(-1), "aria-label": "-" }, icon("minus")), val,
    h("button.icon", { onclick: () => step(1), "aria-label": "+" }, icon("plus")));

  const ingList = h("div.r-ings", rec.ingredients.map((i) => {
    if (i.heading) return h("div.r-sub", i.heading);
    const subs = i.subs?.length ? h("div.r-subs.hidden", h("b", `${t("Substitutions")}: `), i.subs.join(" · ")) : null;
    return h("div.r-ing",
      h("div.line", h("span.txt", i.text),
        subs && h("button.icon.ghost", { title: t("Substitutions"), onclick: () => subs.classList.toggle("hidden") }, "⇄"),
        h("button.icon.ghost", { title: t("Add to shopping list"), onclick: (e) => act(async () => {
          await P("shopping", { body: { name: i.text } }); toast(t("Added to the shopping list"));
        }, e.currentTarget) }, icon("cart"))),
      subs);
  }));

  const steps = [];
  let n = 0;
  for (const s of rec.steps) {
    if (s.startsWith("# ")) { steps.push(h("div.r-sub", s.slice(2))); continue; }
    steps.push(h("div.r-step", h("span.n", ++n), h("p", s)));
  }

  const notes = h("textarea", { rows: 3, placeholder: t("Your notes: tweaks, tips, family verdict…"), value: rec.notes });
  notes.addEventListener("change", () => act(async () => { await P(`recipe/${id}`, { method: "PUT", body: { notes: notes.value } }); rec.notes = notes.value; }));

  const tagInput = h("input", { placeholder: t("Tags (comma separated)"), value: rec.tags.join(", ") });
  tagInput.addEventListener("change", () => upd({ tags: tagInput.value }));

  fill(view,
    h("div.r-hero", rec.image ? h("img", { src: rec.image, alt: "" }) : h("div.ph", icon("utensils")),
      h("button.icon.back", { onclick: () => go("#/recipes/mine"), "aria-label": t("Back") }, icon("left"))),
    h("div.r-titlebar", h("h1", rec.title), heart),
    h("div.row.wrap.small.muted", { style: { gap: "8px" } },
      kcalChip, meta && h("span", meta), rec.edited && h("span.chip.warn", t("edited")),
      rec.source_url && h("a", { href: rec.source_url, target: "_blank", rel: "noopener" }, icon("link"), " ", rec.site || t("Source"))),
    h("div.row.wrap.r-actions", { style: { gap: "8px" } },
      h("button", { onclick: () => editMode(view, rec, ctx) }, icon("edit"), t("Edit")),
      hasPlugin("sync") && h("button", { onclick: (e) => act(async () => {
        const r = await api("p/sync/recipes/share", { body: { recipe_id: rec.id } });
        const el = h("div.toast", `${t("Recipe shared.")} `, h("a", { href: `#/social/recipe/${encodeURIComponent(r.slug)}` }, t("View")));
        document.getElementById("toasts").append(el);
        setTimeout(() => el.remove(), 7000);
      }, e.currentTarget) }, icon("users"), t("Share with friends")),
      rec.has_original && rec.edited && h("button", { onclick: () => confirm(t("Go back to the original version? Your edits to title, ingredients and method will be lost.")) && act(async () => {
        await P(`recipe/${id}/reset`, { body: {} }); ctx.servings = 0; await ctx.reload(); ctx.paint();
      }) }, icon("refresh"), t("Reset to original")),
      h("button.danger.r-del", { onclick: () => confirm(t("Delete this recipe from your hub?")) && act(async () => {
        await P(`recipe/${id}`, { method: "DELETE" }); toast(t("Recipe deleted")); go("#/recipes/mine");
      }) }, icon("trash"), t("Delete"))),
    h("div.row.wrap", { style: { gap: "14px" } }, stars, h("div.spacer"),
      h("button", { onclick: () => logPortion(rec) }, icon("utensils"), t("Log a portion"))),
    h("div.r-cols",
      h("div.card.stack", { style: { gap: "10px" } },
        h("div.row", h("h2", t("Ingredients")), h("div.spacer"), h("span.small.muted", t("Servings")), sv),
        ingList,
        h("button", { onclick: (e) => act(async () => {
          await P("shopping", { body: { recipe_id: rec.id, servings: rec.servings } }); toast(t("Added to the shopping list"));
        }, e.currentTarget) }, icon("cart"), t("Add to shopping list"))),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Method")), steps.length ? steps : h("div.muted", "–"))),
    h("div.card.stack", { style: { gap: "8px" } }, h("h2", t("Personal notes")), notes,
      h("label.field", t("Tags (comma separated)").split(" (")[0], tagInput)));
}

function editMode(view, rec, ctx) {
  const title = h("input", { value: rec.title });
  const sv = h("input", { type: "number", min: 1, step: 1, value: rec.base_servings });
  const orig = rec.kcal_est ? "" : String(rec.kcal || "");
  const kcal = h("input", { type: "number", min: 0, value: orig, placeholder: rec.kcal_est ? `~${rec.kcal}` : "" });
  const ing = h("textarea", { rows: Math.min(18, rec.ingredient_lines.length + 2), value: rec.ingredient_lines.join("\n") });
  const steps = h("textarea", { rows: Math.min(22, rec.steps.length * 3 + 2), value: rec.steps.join("\n") });
  fill(view, h("h1", t("Edit")), h("div.card.stack",
    h("label.field", t("Title"), title), h("label.field", t("Servings"), sv),
    h("label.field", t("Calories per serving (empty = automatic)"), kcal), h("label.field", t("Ingredients (one per line, '# Heading' for a group)"), ing), h("label.field", t("Method (one step per line)"), steps),
    h("div.row", { style: { gap: "8px" } },
      h("button.primary", { onclick: (e) => act(async () => {
        const patch = { title: title.value, servings: +sv.value, ingredients: ing.value, steps: steps.value };
        if (kcal.value !== orig) patch.kcal = kcal.value === "" ? null : +kcal.value;
        ctx.servings = +sv.value;
        await ctx.reload(patch); ctx.paint();
      }, e.currentTarget) }, icon("check"), t("Save")),
      h("button", { onclick: () => ctx.paint() }, t("Cancel")))));
}

function logPortion(rec) {
  const n = h("input", { type: "number", min: 0.25, step: 0.25, value: 1 });
  const k = h("input", { type: "number", min: 0, placeholder: rec.kcal ? String(rec.kcal) : "kcal",
                         value: rec.kcal ? "" : "" });
  const total = h("div.small.muted");
  const upd = () => { const per = +k.value || rec.kcal || 0; total.textContent = per ? `= ${Math.round(per * (+n.value || 0))} kcal` : ""; };
  n.oninput = k.oninput = upd; upd();
  const s = sheet(t("Log a portion"), [
    h("div", h("b", rec.title)),
    h("label.field", t("Portions eaten"), n),
    h("label.field", `${t("kcal per serving")}${rec.kcal ? "" : ""}`, k), total,
    h("button.primary.lg", { onclick: (e) => act(async () => {
      await P("log", { body: { recipe_id: rec.id, servings: +n.value, ...(k.value ? { kcal: +k.value } : {}), day: localDay() } });
      s.close(); toast(t("Added to today's log"));
    }, e.currentTarget) }, t("Log a portion"))]);
}

// ---------------------------------------------------------------- shopping

async function renderShopping(el) {
  const list = h("div");
  const input = h("input", { placeholder: t("Add an item (e.g. 2 kg potatoes)…"), enterkeyhint: "done" });
  const addForm = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!input.value.trim()) return;
    act(async () => { const r = await P("shopping", { body: { name: input.value } }); input.value = ""; paint(r.items); }); } },
    h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("Add")));
  fill(el, addForm, list);
  const call = (path, opts) => act(async () => paint((await P(path, opts)).items));

  function paint(items) {
    const open = items.filter((i) => !i.checked), done = items.filter((i) => i.checked);
    const row = (i) => {
      const subs = i.subs?.length ? h("div.r-subs.hidden", h("b", `${t("Substitutions")}: `), i.subs.join(" · ")) : null;
      return h(`div.item.r-shop${i.checked ? ".done" : ""}`,
        h("input", { type: "checkbox", checked: i.checked, onchange: (e) => call(`shopping/${i.id}`, { method: "PATCH", body: { checked: e.target.checked } }) }),
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("div.txt", i.text),
          i.recipes.length ? h("div.tiny.muted.ellipsis", t("from {recipes}", { recipes: i.recipes.join(", ") })) : null, subs),
        subs && h("button.icon.ghost", { title: t("Substitutions"), onclick: () => subs.classList.toggle("hidden") }, "⇄"),
        h("button.icon.ghost", { onclick: () => call(`shopping/${i.id}`, { method: "DELETE" }), "aria-label": t("Delete") }, icon("x")));
    };
    fill(list,
      items.length ? [
        h("div.list", [...open, ...done].map(row)),
        h("div.row", { style: { gap: "8px", marginTop: "12px" } },
          done.length ? h("button", { onclick: () => call("shopping", { method: "DELETE" }) }, icon("check"), t("Clear checked")) : null,
          h("button.danger", { onclick: () => confirm(t("Clear all") + "?") && call("shopping?all=1", { method: "DELETE" }) }, icon("trash"), t("Clear all")))]
        : h("div.empty", t("Nothing to buy. Open a recipe and add its ingredients.")));
  }
  paint(await P("shopping"));
}

// ---------------------------------------------------------------- today

async function renderToday(el) {
  let day = localDay();
  const body = h("div.stack", { style: { gap: "16px" } });
  fill(el, body);
  const [recipes] = await Promise.all([P("recipes")]);

  async function paint() {
    const [log, nut] = await Promise.all([P(`log?day=${day}`), P("nutrition")]);
    const pct = Math.min(100, Math.round(log.total / log.goal * 100));
    const diff = Math.round(log.goal - log.total);
    const dayInput = h("input", { type: "date", value: day, max: localDay(), style: { width: "auto" },
      onchange: (e) => { day = e.target.value || localDay(); paint(); } });

    // add forms
    const mName = h("input", { placeholder: t("What you ate") });
    const mKcal = h("input", { type: "number", min: 0, placeholder: t("kcal"), style: { maxWidth: "110px" } });
    const manual = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); act(async () => {
      await P("log", { body: { day, name: mName.value, kcal: +mKcal.value } }); paint(); }); } },
      mName, mKcal, h("button.primary", { type: "submit" }, icon("plus")));
    const pick = h("select", h("option", { value: "" }, t("Pick a recipe…")),
      recipes.map((r) => h("option", { value: r.id }, r.title + (r.kcal ? ` (${kcalText(r)})` : ""))));
    const por = h("input", { type: "number", min: 0.25, step: 0.25, value: 1, style: { maxWidth: "90px" } });
    const fromBook = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!pick.value) return; act(async () => {
      await P("log", { body: { day, recipe_id: +pick.value, servings: +por.value } }); paint(); }); } },
      pick, por, h("button.primary", { type: "submit" }, icon("plus")));

    const max = Math.max(nut.goal * 1.15, ...nut.days.map((d) => d.kcal));
    const bars = h("div.r-bars", nut.days.map((d) => h("div.bar", { title: `${d.day}: ${Math.round(d.kcal)} kcal`,
      onclick: () => { day = d.day; paint(); } },
      h("div.col", h("div.goal", { style: { bottom: `${nut.goal / max * 100}%` } }), h(`div.fill${d.kcal > nut.goal ? ".over" : ""}${d.day === day ? ".sel" : ""}`, { style: { height: `${d.kcal / max * 100}%` } })),
      h("div.lbl", new Date(d.day + "T12:00").toLocaleDateString(locale(), { weekday: "short" })),
      h("div.val", d.kcal ? Math.round(d.kcal) : ""))));

    fill(body,
      h("div.card.stack", { style: { gap: "10px" } },
        h("div.row", { style: { gap: "10px" } }, dayInput, h("div.spacer"), h("span.muted.small", `${t("goal {n} kcal", { n: Math.round(log.goal) })}`)),
        h("div.r-total", h("span.big", Math.round(log.total)), h("span.muted", " " + t("kcal eaten"))),
        h("div.progress", h(`div${log.total > log.goal ? ".over" : ""}`, { style: { width: `${pct}%` } })),
        h("div.small.muted", diff >= 0 ? t("{n} kcal left", { n: diff }) : t("{n} kcal over", { n: -diff }))),
      log.entries.length
        ? h("div.list", log.entries.map((e) => h("div.item",
          h("div.stack", { style: { gap: "2px", flex: 1, minWidth: 0 } }, h("div.ellipsis", e.name),
            h("div.tiny.muted", [e.time, e.servings && `× ${e.servings}`, e.est && "~"].filter(Boolean).join(" · "))),
          h("b", `${e.est ? "~" : ""}${Math.round(e.kcal)}`), h("span.muted.small", "kcal"),
          h("button.icon.ghost", { onclick: () => act(async () => { await P(`log/${e.id}`, { method: "DELETE" }); paint(); }), "aria-label": t("Delete") }, icon("x")))))
        : h("div.empty", t("Nothing logged for this day.")),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("From your recipes")), fromBook, h("h2", t("Manual entry")), manual),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Last 7 days")), bars));
  }
  await paint();
}
