// Recipes: discover/import, personal recipe book, shopping list, calorie log (hub/plugins/recipes).
// Routes: #/recipes[/discover|week|mine|shopping|today|recipe/<id>]
import { api, h, icon, toast, sheet, ensureCss, act, debounce, go, hasPlugin, t, locale, fmt, bus } from "../app.js";

const P = (path, opts) => api(`p/recipes/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const emptyState = (ic, text, ...actions) => h("div.empty.r-empty", h("div.r-empty-ic", icon(ic)), h("p", text),
  actions.length ? h("div.r-empty-act", actions) : null);
const loading = () => h("div.empty", h("div.spin"));

const fmtMin = (m) => !m ? "" : m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60}` : ""}` : `${m} ${t("min")}`;
const kcalText = (r) => r.kcal ? `${r.kcal_est ? "~" : ""}${r.kcal} kcal` : "";
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const isUrl = (s) => /^https?:\/\/\S+$/i.test(s.trim());
const FLAG = { it: "🇮🇹", en: "🇬🇧" };

let status = { language: "it", goal: 2000 };

const TABS = ["discover", "week", "mine", "shopping", "today"];
const TAB_LABEL = () => ({ discover: t("Discover"), week: t("Week"), mine: t("My recipes"), shopping: t("Shopping list"), today: t("Calories") });

export default {
  async render(root, args) {
    ensureCss(`/static/pages/recipes.css?v=${window.CASA_V}`);
    try { status = await P("status"); } catch (e) { fill(root, h("div.empty", e.message)); return; }
    const [view, id] = args;
    if (view === "recipe") { await renderDetail(root, id); return; }

    const tab = TABS.includes(view) ? view : "discover";
    const body = h("div.r-body");
    fill(root,
      h("div.r-head", h("h1", t("Kitchen")),
        h("button.primary", { onclick: () => newRecipe() }, icon("plus"), t("New recipe"))),
      h("div.tabs", TABS.map((k) =>
        h(`button${k === tab ? ".active" : ""}`, { onclick: () => go(`#/recipes/${k}`) }, TAB_LABEL()[k]))),
      body);
    ({ discover: renderDiscover, week: renderWeek, mine: renderMine, shopping: renderShopping, today: renderToday })[tab](body);
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
    input.blur();
    try { sessionStorage.setItem("r-q", q); } catch {}
    if (isUrl(q)) return importRecipe({ url: q }, btn);
    fill(out, loading());
    try { showResults(out, await P(`search?q=${enc(q)}`), q); }
    catch (err) { fill(out, h("div.empty", err.message)); }
  };
  fill(el, cookCard(el), h("form.r-search", { onsubmit: submit }, h("div.r-input", icon("search"), input), btn), out);
  if (input.value.trim() && !isUrl(input.value)) submit(new Event("submit"));
  else intro(out, input, submit);
}

function intro(out, input, submit) {
  const ideas = [t("pasta"), t("risotto"), t("chicken"), t("vegetables"), t("desserts"), t("soups")];
  const recent = h("div");
  fill(out, h("div.r-empty.r-intro", h("div.r-empty-ic", icon("utensils")),
    h("p", t("Search for a recipe or paste a link: we save it in the app.")),
    h("div.r-ideas", ideas.map((w) => h("button.chipbtn", { onclick: () => { input.value = w; input.dispatchEvent(new Event("input")); submit(new Event("submit")); } }, w)))),
    recent);
  P("recipes").then((items) => {
    if (!items.length) return;
    items = [...items].sort((a, b) => (b.updated || 0) - (a.updated || 0) || b.id - a.id).slice(0, 6);
    fill(recent, h("h2.r-sec", t("Your latest recipes")),
      h("div.r-grid", items.map((r) => card({ ...r, meta: [kcalText(r), fmtMin(r.total_min)].filter(Boolean).join(" · ") || r.site },
        () => go(`#/recipes/recipe/${r.id}`)))));
  }).catch(() => {});
}

function showResults(out, res, q) {
  const warn = Object.keys(res.errors || {}).length
    ? h("div.r-banner", "⚠️", h("div", `${t("Some sources didn't answer")}: ${Object.entries(res.errors).map(([k, v]) => `${k} (${v})`).join(", ")}`)) : null;
  let which = "all";
  const grid = h("div.r-grid");
  const chips = h("div.r-chips");
  const draw = () => {
    const list = res.results.filter((r) => which === "all" || (which === "it" ? r.cuisine === "italian" : r.cuisine !== "italian"));
    fill(grid, ...(list.length ? list.map((r) => {
      const c = card({ image: r.image, title: r.title, meta: `${FLAG[r.lang] || ""} ${r.source || r.subtitle || r.site}` },
        () => importRecipe(r.mealdb ? { mealdb: r.mealdb } : { url: r.url }, c),
        r.saved ? h("span.r-badge", icon("check")) : null);
      return c;
    }) : [emptyState("search", `${t("Nothing found.")} “${q}”`)]));
    fill(chips, ...[["all", t("All sources")], ["it", t("Italian sites")], ["world", t("World cuisines")]].map(([k, label]) =>
      h(`button.chipbtn${which === k ? ".active" : ""}`, { type: "button", onclick: () => { which = k; draw(); } }, label)));
  };
  draw();
  fill(out, warn, res.results.length ? chips : null, res.results.length ? grid : emptyState("search", `${t("Nothing found.")} “${q}”`));
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

// ---------------------------------------------------------------- cook with what we have

const norm = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
const jsonKey = (k, d) => { try { return JSON.parse(sessionStorage.getItem(k)) ?? d; } catch { return d; } };

function cookCard(el) {
  return h("button.r-cookcard", { type: "button", onclick: () => renderCook(el) },
    h("span.r-cookic", icon("utensils")),
    h("span.r-cooktx", h("b", t("What can I cook with what I have?")), h("span", t("Type 2-3 ingredients and see what you can make"))),
    h("span.r-cookgo", icon("search")));
}

async function renderCook(el) {
  let chips = jsonKey("r-cook", []);
  let usePantry = jsonKey("r-cook-pantry", true);
  let pantry = [], known = [], seq = 0;
  const save = () => { try { sessionStorage.setItem("r-cook", JSON.stringify(chips)); sessionStorage.setItem("r-cook-pantry", JSON.stringify(usePantry)); } catch {} };
  const input = h("input", { placeholder: t("Add an ingredient (e.g. zucchini)"), enterkeyhint: "done", autocomplete: "off", autocapitalize: "none" });
  const chipBox = h("div.r-cchips"), sugg = h("div.r-ideas.r-sugg"), pantryBtn = h("button.r-pantrybtn", { type: "button" });
  const toggle = h("input", { type: "checkbox", checked: usePantry, onchange: (e) => { usePantry = e.target.checked; save(); run(); } });
  const local = h("div"), web = h("div");

  const add = (txt) => {
    const parts = String(txt).split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
    let changed = false;
    for (const x of parts) if (!chips.some((c) => norm(c) === norm(x))) { chips.push(x); changed = true; }
    input.value = ""; paintSugg();
    if (changed) { paintChips(); run(); }
  };
  const drop = (i) => { chips.splice(i, 1); paintChips(); run(); };
  function paintChips() {
    save();
    fill(chipBox, chips.map((c, i) => h("span.r-cchip", c,
      h("button", { type: "button", "aria-label": t("Remove {name}", { name: c }), onclick: () => drop(i) }, icon("x")))));
  }
  function paintSugg() {
    const q = norm(input.value);
    const list = q ? known.filter((k) => norm(k).includes(q) && !chips.some((c) => norm(c) === norm(k))).slice(0, 6) : [];
    fill(sugg, list.map((k) => h("button.chipbtn", { type: "button", onclick: () => { add(k); input.focus(); } }, icon("plus"), k)));
  }
  const paintPantryBtn = () => fill(pantryBtn, icon("storage"), `${t("Pantry")} (${pantry.length})`);
  pantryBtn.onclick = () => pantrySheet(pantry, (items) => { pantry = items; paintPantryBtn(); paintToggle(); run(); });
  const toggleLabel = h("label.r-pantrytog", toggle, `${t("Use the pantry")} (0)`);
  function paintToggle() { fill(toggleLabel, toggle, `${t("Use the pantry")} (${pantry.length})`); }

  input.addEventListener("input", () => { if (input.value.includes(",")) add(input.value); else paintSugg(); });
  const form = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) add(input.value); } },
    h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("Add")));

  async function run() {
    const my = ++seq;
    if (!chips.length) { fill(web); return showEmpty(); }
    fill(local, loading()); fill(web);
    const q = chips.join(" ");
    P("cook-with", { body: { ingredients: chips, use_pantry: usePantry } }).then((r) => {
      if (my !== seq) return;
      fill(local, h("h2.r-sec", t("From your recipes")),
        r.results.length ? h("div.r-clist", r.results.map(cookResult))
          : h("div.r-banner", icon("search"), h("div", t("None of your saved recipes fits yet. Try the web ideas below."))));
    }).catch((e) => my === seq && fill(local, h("div.empty", e.message)));
    fill(web, h("h2.r-sec", t("Ideas from the web")), loading());
    P(`search?q=${enc(q)}`).then((res) => {
      if (my !== seq) return;
      const items = (res.results || []).slice(0, 12);
      fill(web, h("h2.r-sec", t("Ideas from the web")), items.length
        ? h("div.r-grid", items.map((r) => {
          const c = card({ image: r.image, title: r.title, meta: `${FLAG[r.lang] || ""} ${r.source || r.subtitle || r.site}` },
            () => importRecipe(r.mealdb ? { mealdb: r.mealdb } : { url: r.url }, c), r.saved ? h("span.r-badge", icon("check")) : null);
          return c;
        }))
        : h("p.muted", t("Nothing found.")));
    }).catch(() => my === seq && fill(web));
  }
  function showEmpty() {
    const ex = [t("zucchini, eggs"), t("pasta, tomatoes"), t("potatoes, onions"), t("rice, mushrooms")];
    fill(local, h("div.r-empty.r-intro", h("div.r-empty-ic", icon("utensils")),
      h("p", t("Type what you have: we look for what you can cook, from your recipes and from the web.")),
      h("div.r-ideas", ex.map((x) => h("button.chipbtn", { type: "button", onclick: () => { chips = []; add(x); } }, x)))));
  }

  fill(el,
    h("div.r-cookhead", h("button.icon.ghost", { type: "button", "aria-label": t("Back"), onclick: () => renderDiscover(el) }, icon("back")),
      h("h2", t("What can I cook with what I have?"))),
    form, sugg, chipBox,
    h("div.r-cookopts", toggleLabel, pantryBtn), local, web);
  paintChips(); paintToggle(); paintPantryBtn(); run();
  input.focus({ preventScroll: true });
  try {
    pantry = (await P("pantry")).items; paintToggle(); paintPantryBtn(); if (chips.length) run();
    known = (await P("ingredients")).items;
  } catch {}
}

function cookResult(r) {
  const missing = r.missing || [], subs = r.substitutes || [];
  const status = !missing.length && !subs.length ? h("span.r-ok", icon("check"), t("You have everything"))
    : !missing.length ? h("span.r-ok.r-near", icon("check"), t("You have everything (with substitutes)"))
    : h("span.r-miss", t("Missing: {items}", { items: missing.join(", ") }));
  const addBtn = missing.length ? h("button", { type: "button", onclick: (e) => act(async () => {
    for (const m of missing) await P("shopping", { body: { name: m } });
    toast(t("Added to the shopping list"));
  }, e.currentTarget) }, icon("cart"), t("Add missing to the shopping list")) : null;
  return h("div.r-cres",
    h("div.r-cmain", { onclick: () => go(`#/recipes/recipe/${r.id}`) },
      r.image ? h("img", { src: r.image, loading: "lazy", alt: "" }) : h("div.ph", icon("utensils")),
      h("div.r-cinfo", h("div.t", r.title), status,
        subs.map((s) => h("div.r-sub", t("≈ {have} instead of {need}", { have: s.have, need: s.need }))))),
    addBtn);
}

function pantrySheet(items, onChange) {
  let list = [...items];
  const box = h("div.r-cchips"), input = h("input", { placeholder: t("Add to the pantry (e.g. rice)"), enterkeyhint: "done", autocomplete: "off" });
  const paint = () => fill(box, list.length ? list.map((x) => h("span.r-cchip", x,
    h("button", { type: "button", "aria-label": t("Remove {name}", { name: x }), onclick: () => change("remove", [x]) }, icon("x"))))
    : h("p.muted", t("The pantry is empty.")));
  const change = (what, its) => act(async () => {
    list = (await P(`pantry/${what}`, { body: { items: its } })).items; paint(); onChange(list);
  });
  const s = sheet(t("Pantry"), [
    h("p.muted", t("What we always have at home. Salt, pepper, oil, water, sugar and vinegar are already counted.")),
    h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) { change("add", [input.value]); input.value = ""; } } },
      h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("Add"))),
    box,
    h("button", { type: "button", onclick: (e) => act(async () => {
      const taken = (await P("shopping")).items.filter((i) => i.checked).map((i) => i.name);
      if (!taken.length) return toast(t("No ticked items in the shopping list"));
      await change("add", taken);
    }, e.currentTarget) }, icon("cart"), t("Add ticked shopping items"))]);
  paint();
  return s;
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
      : state.fav || state.tag || state.q
        ? emptyState("search", t("Nothing found."), h("button", { onclick: () => { state.fav = false; state.tag = ""; state.q = ""; q.value = ""; refresh(); } }, t("Show all")))
        : emptyState("utensils", t("No saved recipes yet. Search or import your first one!"),
          h("button.primary", { onclick: () => go("#/recipes/discover") }, icon("search"), t("Discover recipes")),
          h("button", { onclick: () => newRecipe() }, icon("plus"), t("New recipe"))));
  }
  q.addEventListener("input", debounce(() => { state.q = q.value; refresh(); }, 250));
  await refresh();
}

// ---------------------------------------------------------------- detail

// servings the user picked per recipe in this session (otherwise the household default applies)
const mySessionServings = new Map();

async function renderDetail(root, id) {
  fill(root, loading());
  let rec;
  let servings = mySessionServings.get(id) || 0;
  try {
    if (!servings) { if (!status.default_servings) status = { ...status, ...(await P("status")) }; servings = status.default_servings || 0; }
    rec = await P(servings ? `recipe/${id}?servings=${servings}` : `recipe/${id}`);
  } catch (e) { fill(root, h("div.empty", e.message)); return; }
  const view = h("div.r-detail");
  fill(root, view);
  const reload = async (patch) => {
    if (patch) await P(`recipe/${id}`, { method: "PUT", body: patch });
    rec = await P(servings ? `recipe/${id}?servings=${servings}` : `recipe/${id}`);
    servings = rec.servings;
    mySessionServings.set(id, servings);
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
  const val = h("span.val", fmt.num(target, 1));
  const orig = h("span.small.muted.r-orig", rec.base_servings !== rec.servings ? t("original recipe: {n}", { n: fmt.num(rec.base_servings, 1) }) : "");
  const apply = debounce(() => { ctx.servings = target; act(async () => { await ctx.reload(); ctx.paint(); }); }, 350);
  const step = (d) => { target = Math.max(1, Math.round(target) + d); val.textContent = fmt.num(target, 1); apply(); };
  const sv = h("div.r-portions",
    h("span.lbl", t("Servings")),
    h("div.stepper.big",
      h("button.icon", { onclick: () => step(-1), "aria-label": "-" }, icon("minus")), val,
      h("button.icon", { onclick: () => step(1), "aria-label": "+" }, icon("plus"))),
    orig);

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
    sv,
    h("div.row.wrap.small.muted", { style: { gap: "8px" } },
      kcalChip, meta && h("span", meta), rec.edited && h("span.chip.warn", t("edited")),
      rec.source_url && h("a", { href: rec.source_url, target: "_blank", rel: "noopener" }, icon("link"), " ", rec.site || t("Source"))),
    h("div.row.wrap.r-actions", { style: { gap: "8px" } },
      h("button.primary", { onclick: () => addToMenu(rec) }, icon("calendar"), t("Add to the menu")),
      h("button", { onclick: () => editMode(view, rec, ctx) }, icon("edit"), t("Edit")),
      hasPlugin("sync") && h("button", { onclick: (e) => act(async () => {
        const r = await api("p/sync/recipes/share", { body: { recipe_id: rec.id } });
        const el = h("div.toast", `${t("Recipe shared.")} `, h("a", { href: `#/social/recipe/${encodeURIComponent(r.slug)}` }, t("View")));
        document.getElementById("toasts").append(el);
        setTimeout(() => el.remove(), 7000);
      }, e.currentTarget) }, icon("users"), t("Share with friends")),
      rec.has_original && rec.edited && h("button", { onclick: () => confirm(t("Go back to the original version? Your edits to title, ingredients and method will be lost.")) && act(async () => {
        await P(`recipe/${id}/reset`, { body: {} }); mySessionServings.delete(id); ctx.servings = status.default_servings || 0; await ctx.reload(); ctx.paint();
      }) }, icon("refresh"), t("Reset to original")),
      h("button.danger.r-del", { onclick: () => confirm(t("Delete this recipe from your hub?")) && act(async () => {
        await P(`recipe/${id}`, { method: "DELETE" }); toast(t("Recipe deleted")); go("#/recipes/mine");
      }) }, icon("trash"), t("Delete"))),
    h("div.row.wrap", { style: { gap: "14px" } }, stars, h("div.spacer"),
      h("button", { onclick: () => logPortion(rec) }, icon("utensils"), t("Log a portion"))),
    h("div.r-cols",
      h("div.card.stack", { style: { gap: "10px" } },
        h("h2", t("Ingredients")),
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
  const ing = h("textarea.r-edit", { rows: Math.max(8, Math.min(24, rec.ingredient_lines.length + 3)), value: rec.ingredient_lines.join("\n") });
  const steps = h("textarea.r-edit", { rows: Math.max(10, Math.min(30, rec.steps.length * 3 + 3)), value: rec.steps.join("\n") });
  const bar = h("div.r-editbar",
    h("button", { onclick: () => ctx.paint() }, t("Cancel")),
    h("button.primary", { onclick: (e) => act(async () => {
      const patch = { title: title.value, servings: +sv.value, ingredients: ing.value, steps: steps.value };
      if (kcal.value !== orig) patch.kcal = kcal.value === "" ? null : +kcal.value;
      ctx.servings = +sv.value === rec.base_servings ? ctx.servings : +sv.value;
      await ctx.reload(patch); ctx.paint();
    }, e.currentTarget) }, icon("check"), t("Save")));
  fill(view, bar, h("h1", t("Edit")), h("div.card.stack",
    h("label.field", t("Title"), title), h("label.field", t("Servings in the original recipe"), sv),
    h("label.field", t("Ingredients (one per line, '# Heading' for a group)"), ing), h("label.field", t("Method (one step per line)"), steps),
    h("label.field", t("Calories per serving (empty = automatic)"), kcal)));
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
  const input = h("input", { placeholder: t("Add (e.g. milk)"), enterkeyhint: "done" });
  const addForm = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!input.value.trim()) return;
    act(async () => { const r = await P("shopping", { body: { name: input.value } }); input.value = ""; paint(r.items); }); } },
    h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("Add")));
  fill(el, addForm, list);
  const call = (path, opts) => act(async () => paint((await P(path, opts)).items));

  function paint(items) {
    const open = items.filter((i) => !i.checked), done = items.filter((i) => i.checked);
    const row = (i) => {
      const subs = i.subs?.length ? h("div.r-subs.hidden", h("b", `${t("Substitutions")}: `), i.subs.join(" · ")) : null;
      return h(`div.item.r-shop${i.checked ? ".done" : ""}`, { onclick: (e) => {
        if (e.target.closest("button")) return;
        call(`shopping/${i.id}`, { method: "PATCH", body: { checked: !i.checked } }); } },
        h("input", { type: "checkbox", checked: i.checked, "aria-label": i.text, onclick: (e) => e.stopPropagation(),
          onchange: (e) => call(`shopping/${i.id}`, { method: "PATCH", body: { checked: e.target.checked } }) }),
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("div.txt", i.text),
          i.recipes.length ? h("div.tiny.muted.ellipsis", t("from {recipes}", { recipes: i.recipes.join(", ") })) : null, subs),
        subs && h("button.icon.ghost", { title: t("Substitutions"), onclick: () => subs.classList.toggle("hidden") }, "⇄"),
        h("button.icon.ghost", { onclick: () => call(`shopping/${i.id}`, { method: "DELETE" }), "aria-label": t("Delete") }, icon("x")));
    };
    fill(list,
      items.length ? [
        open.length ? h("div.list", open.map(row)) : h("div.empty.r-allin", t("Everything taken!")),
        done.length ? h("div.r-donehead", h("h2.r-sec", `${t("Taken")} (${done.length})`),
          h("button", { onclick: () => call("shopping", { method: "DELETE" }) }, icon("trash"), t("Clear taken"))) : null,
        done.length ? h("div.list", done.map(row)) : null,
        h("div.row", { style: { marginTop: "12px" } },
          h("button.danger", { onclick: () => confirm(t("Clear all") + "?") && call("shopping?all=1", { method: "DELETE" }) }, icon("trash"), t("Clear all")))]
        : emptyState("cart", t("Nothing to buy. Open a recipe and add its ingredients."),
          h("button.primary", { onclick: () => go("#/recipes/mine") }, icon("utensils"), t("My recipes"))));
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
    const bars = h("div.r-bars", nut.days.map((d) => h("div.bar", { title: `${d.day}: ${fmt.num(d.kcal)} kcal`,
      onclick: () => { day = d.day; paint(); } },
      h("div.col", h("div.goal", { style: { bottom: `${nut.goal / max * 100}%` } }), h(`div.fill${d.kcal > nut.goal ? ".over" : ""}${d.day === day ? ".sel" : ""}`, { style: { height: `${d.kcal / max * 100}%` } })),
      h("div.lbl", new Date(d.day + "T12:00").toLocaleDateString(locale(), { weekday: "short" })),
      h("div.val", d.kcal ? fmt.num(d.kcal) : ""))));

    fill(body,
      h("div.card.stack", { style: { gap: "10px" } },
        h("div.row", { style: { gap: "10px" } }, dayInput, h("div.spacer"), h("span.muted.small", `${t("goal {n} kcal", { n: fmt.num(log.goal) })}`)),
        h("div.r-total", h("span.big", fmt.num(log.total)), h("span.muted", " " + t("kcal eaten"))),
        h("div.progress", h(`div${log.total > log.goal ? ".over" : ""}`, { style: { width: `${pct}%` } })),
        h("div.small.muted", diff >= 0 ? t("{n} kcal left", { n: fmt.num(diff) }) : t("{n} kcal over", { n: fmt.num(-diff) }))),
      log.entries.length
        ? h("div.list", log.entries.map((e) => h("div.item",
          h("div.stack", { style: { gap: "2px", flex: 1, minWidth: 0 } }, h("div.ellipsis", e.name),
            h("div.tiny.muted", [e.time, e.servings && `× ${e.servings}`, e.est && "~"].filter(Boolean).join(" · "))),
          h("b", `${e.est ? "~" : ""}${fmt.num(e.kcal)}`), h("span.muted.small", "kcal"),
          h("button.icon.ghost", { onclick: () => act(async () => { await P(`log/${e.id}`, { method: "DELETE" }); paint(); }), "aria-label": t("Delete") }, icon("x")))))
        : emptyState("flame", t("Nothing logged for this day."),
          h("button.primary", { onclick: () => (recipes.length ? pick : mName).focus() }, icon("plus"), t("Add what you ate"))),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("From your recipes")), fromBook, h("h2", t("Manual entry")), manual),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Last 7 days")), bars));
  }
  await paint();
}

// ---------------------------------------------------------------- week planner

const MEAL_LABEL = () => ({ lunch: t("Lunch"), dinner: t("Dinner") });
const dateOf = (s) => new Date(`${s}T12:00:00`);
const addDays = (s, n) => { const d = dateOf(s); d.setDate(d.getDate() + n); return localDay(d); };
const mondayOf = (s) => addDays(s, -((dateOf(s).getDay() + 6) % 7));
const weekLabel = (mon) => {
  const a = dateOf(mon), b = dateOf(addDays(mon, 6));
  return a.getMonth() === b.getMonth()
    ? `${a.getDate()}–${fmt.date(b, { weekday: false })}`
    : `${fmt.date(a, { weekday: false })} – ${fmt.date(b, { weekday: false })}`;
};
const people = (n) => t("{n} people", { n });
let recipeCache = null;
const myRecipes = async (fresh) => (recipeCache && !fresh ? recipeCache : (recipeCache = await P("recipes")));

function toastLink(msg, label, href) {
  const el = h("div.toast", `${msg} `, h("a", { href }, label));
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), 6000);
}

async function renderWeek(el) {
  const today = localDay();
  let mon = mondayOf(today), plan = null;
  const head = h("div.w-nav"), actions = h("div.w-actions"), days = h("div.w-days");
  fill(el, head, actions, days);

  async function load() {
    try { plan = await P(`plan?week=${mon}`); } catch (e) { fill(days, h("div.empty", e.message)); return; }
    paint();
  }
  const reloadSoon = debounce(() => { if (el.isConnected) load(); }, 120);
  const onPlan = (e) => { if (!el.isConnected) bus.removeEventListener("recipes_plan", onPlan); else if (!e.detail || e.detail.week === mon) reloadSoon(); };
  bus.addEventListener("recipes_plan", onPlan);

  const go_ = (m) => { mon = m; load(); };
  function paint() {
    fill(head,
      h("button.icon", { "aria-label": t("Previous week"), onclick: () => go_(addDays(mon, -7)) }, icon("left")),
      h("div.w-label", weekLabel(mon)),
      h("button.icon", { "aria-label": t("Next week"), onclick: () => go_(addDays(mon, 7)) }, icon("right")),
      mon !== mondayOf(today) ? h("button.chipbtn", { onclick: () => go_(mondayOf(today)) }, t("Today")) : null);
    const empty = plan.days.every((d) => !d.lunch && !d.dinner);
    fill(actions,
      h("button.primary.lg.w-shop", { disabled: empty, onclick: (e) => act(async () => {
        const r = await P("plan/shopping", { body: { week: mon } });
        if (!r.added) return toast(t("Nothing to add: plan some recipes first"));
        toastLink(t("{n} ingredients on the list", { n: r.added }), t("Shopping list"), "#/recipes/shopping");
      }, e.currentTarget) }, icon("cart"), t("Add everything to the shopping list")),
      h("div.w-row2",
        h("button", { onclick: (e) => act(async () => {
          const r = await P("plan/fill", { body: { week: mon, meals: ["dinner"] } });
          plan = r; paint();
          toast(!r.filled ? ((await myRecipes()).length ? t("Nothing to fill") : t("Save some recipes first"))
            : r.filled === 1 ? t("One dish added") : t("{n} dishes added", { n: r.filled }));
        }, e.currentTarget) }, icon("utensils"), t("Fill the week")),
        h("button", { onclick: (e) => act(async () => {
          const r = await P("plan/copy", { body: { from_week: addDays(mon, -7), to_week: mon } });
          plan = r; paint();
          if (!r.copied) toast(t("Nothing to copy"));
        }, e.currentTarget) }, icon("calendar"), t("Copy last week"))));
    fill(days, plan.days.map((d) => h(`div.w-day${d.date === today ? ".today" : ""}`,
      h("div.w-dayname", h("b", fmt.date(dateOf(d.date))), d.date === today ? h("span.chip.accent", t("Today")) : null),
      ["lunch", "dinner"].map((m) => slotEl(d, m)))));
  }

  function slotEl(d, meal) {
    const s = d[meal];
    const lbl = h("span.w-meal", MEAL_LABEL()[meal]);
    if (!s) return h("div.w-slot", lbl, h("button.w-add", { onclick: () => pickDish(d.date, meal, load) }, icon("plus"), t("Add")));
    const img = s.image ? h("img", { src: s.image, alt: "", loading: "lazy" }) : h("div.ph", icon("utensils"));
    return h("div.w-slot", lbl, h("button.w-filled", { onclick: () => slotSheet(s, load) },
      h("span.w-img", img), h("span.w-title", s.title), h("span.chip", people(s.servings))));
  }
  await load();
}

function slotSheet(s, reload) {
  let n = s.servings;
  const val = h("span.val", String(n));
  const save = debounce(() => act(async () => {
    await P(`plan/${s.date}/${s.meal}`, { method: "PUT", body: { ...(s.recipe_id ? { recipe_id: s.recipe_id } : { title: s.title }), servings: n, note: s.note } });
    reload();
  }), 350);
  const step = (dlt) => { n = Math.min(50, Math.max(1, n + dlt)); val.textContent = String(n); save(); };
  const sh = sheet(`${MEAL_LABEL()[s.meal]} · ${fmt.date(dateOf(s.date))}`, [
    h("div.w-sheet-title", s.title),
    s.recipe_id ? h("button.lg", { onclick: () => { sh.close(); go(`#/recipes/recipe/${s.recipe_id}`); } }, icon("utensils"), t("Open recipe")) : null,
    h("div.r-portions", h("span.lbl", t("Servings")),
      h("div.stepper.big", h("button.icon", { "aria-label": "-", onclick: () => step(-1) }, icon("minus")), val,
        h("button.icon", { "aria-label": "+", onclick: () => step(1) }, icon("plus")))),
    h("button.lg", { onclick: () => { sh.close(); pickDish(s.date, s.meal, reload); } }, icon("refresh"), t("Replace")),
    h("button.lg.danger", { onclick: (e) => act(async () => {
      await P(`plan/${s.date}/${s.meal}`, { method: "DELETE" }); sh.close(); reload();
    }, e.currentTarget) }, icon("trash"), t("Remove"))]);
}

async function pickDish(date, meal, reload) {
  const q = h("input", { type: "search", placeholder: t("Search your recipes…"), enterkeyhint: "search" });
  const list = h("div.w-pick");
  const sh = sheet(`${MEAL_LABEL()[meal]} · ${fmt.date(dateOf(date))}`, [
    h("div.r-input", icon("search"), q),
    h("div.r-chips", [t("Pizza out"), t("Leftovers"), t("Free")].map((x) =>
      h("button.chipbtn", { onclick: () => choose({ title: x }) }, x)),
      h("button.chipbtn", { onclick: () => writeOwn() }, icon("edit"), t("Write something else…"))),
    list]);
  async function choose(body) {
    try { await P(`plan/${date}/${meal}`, { method: "PUT", body }); sh.close(); reload(); }
    catch (e) { toast(e.message, true); }
  }
  function writeOwn() {
    const inp = h("input", { placeholder: t("What are you eating?"), maxlength: 80, enterkeyhint: "done" });
    fill(list, h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (inp.value.trim()) choose({ title: inp.value.trim() }); } },
      inp, h("button.primary", { type: "submit" }, t("Save"))));
    inp.focus();
  }
  const row = (r) => h("button.w-pickrow", { onclick: () => choose({ recipe_id: r.id }) },
    h("span.w-img", r.image ? h("img", { src: r.image, alt: "", loading: "lazy" }) : h("div.ph", icon("utensils"))),
    h("span.w-title", r.title), r.fav ? h("span.w-fav", icon("heart")) : null);
  let all = [];
  const draw = () => {
    const words = q.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const items = all.filter((r) => words.every((w) => r.title.toLowerCase().includes(w)));
    if (!all.length) return fill(list, h("div.muted.small", t("Save some recipes first")));
    if (!items.length) return fill(list, h("div.muted.small", t("Nothing found.")));
    if (words.length) return fill(list, items.slice(0, 60).map(row));
    const fav = items.filter((r) => r.fav), rest = items.filter((r) => !r.fav);
    fill(list, fav.length ? h("h2.r-sec", t("Favourites")) : null, fav.map(row),
      rest.length ? h("h2.r-sec", fav.length ? t("Recent") : t("Your recipes")) : null, rest.slice(0, 40).map(row));
  };
  q.addEventListener("input", debounce(draw, 80));
  try { all = await myRecipes(true); } catch (e) { toast(e.message, true); }
  draw();
}

// from a recipe page: pick day + meal for the current portions
async function addToMenu(rec) {
  const today = localDay(), mon = mondayOf(today);
  let meal = "dinner";
  const grid = h("div");
  const plans = {};
  const sh = sheet(t("Add to the menu"), [
    h("div.w-sheet-title", rec.title),
    h("div.w-seg", ["lunch", "dinner"].map((m) => h("button", { "data-m": m, onclick: () => { meal = m; paint(); } }, MEAL_LABEL()[m]))),
    grid]);
  async function pick(day) {
    const cur = plans[day]?.[meal];
    if (cur && !confirm(t("This slot already has a dish. Replace it?") + "\n\n" + cur.title)) return;
    try {
      await P(`plan/${day}/${meal}`, { method: "PUT", body: { recipe_id: rec.id, servings: Math.round(rec.servings) } });
      sh.close();
      toastLink(t("Added to the menu"), t("Week"), `#/recipes/week`);
    } catch (e) { toast(e.message, true); }
  }
  function paint() {
    sh.el.querySelectorAll(".w-seg button").forEach((b) => b.classList.toggle("active", b.dataset.m === meal));
    fill(grid, [mon, addDays(mon, 7)].map((m) => h("div.w-pickweek",
      h("div.small.muted", weekLabel(m)),
      h("div.w-pickdays", Array.from({ length: 7 }, (_, i) => {
        const day = addDays(m, i), past = day < today, filled = !!plans[day]?.[meal];
        return h(`button${filled ? ".filled" : ""}${day === today ? ".today" : ""}`, { disabled: past, onclick: () => pick(day) },
          h("span", dateOf(day).toLocaleDateString(locale(), { weekday: "short" })), h("b", dateOf(day).getDate()));
      })))));
  }
  paint();
  try {
    for (const m of [mon, addDays(mon, 7)]) for (const d of (await P(`plan?week=${m}`)).days) plans[d.date] = d;
    paint();
  } catch {}
}
