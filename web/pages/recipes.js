// Recipes: discover/import, personal recipe book, shopping list, calorie log (hub/plugins/recipes).
// Routes: #/recipes[/discover|week|mine|fridge|shopping|today|recipe/<id>]
import { api, h, icon, toast, sheet, ensureCss, act, debounce, go, hasPlugin, t, locale, fmt, bus } from "../app.js";

// Scopri starts fresh; only coming back (browser back / the recipe's back button) from a recipe opened
// from the results restores the query. popstate marks "we arrived by going back".
const ss = (k, v) => { try { v === undefined ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} };
const sg = (k) => { try { return sessionStorage.getItem(k); } catch { return null; } };
window.addEventListener("popstate", () => ss("r-pop", String(Date.now())));

const P = (path, opts) => api(`p/recipes/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const emptyState = (ic, text, ...actions) => h("div.empty.r-empty", h("div.r-empty-ic", icon(ic)), h("p", text),
  actions.length ? h("div.r-empty-act", actions) : null);
const loading = () => h("div.empty", h("div.spin"));

const fmtMin = (m) => !m ? "" : m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60}` : ""}` : `${m} ${t("min")}`;
const kcalText = (r) => r.kcal ? `${r.kcal} kcal` : "";
const cardMeta = (r) => fmtMin(r.total_min) || r.site || "";
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const isUrl = (s) => /^https?:\/\/\S+$/i.test(s.trim());
const FLAG = { it: "🇮🇹", en: "🇬🇧" };

let status = { language: "it", goal: 2000 };

const TABS = ["discover", "week", "mine", "fridge", "shopping", "today"];
const TAB_LABEL = () => ({ discover: t("Discover"), week: t("Week"), mine: t("Recipe book"), fridge: t("Fridge"), shopping: t("Shopping"), today: t("Calories") });

export default {
  async render(root, args) {
    ensureCss(`/static/pages/recipes.css?v=${window.CASA_V}`);
    try { status = await P("status"); } catch (e) { fill(root, h("div.empty", e.message)); return; }
    const [view, id] = args;
    if (view !== "recipe" && view !== "discover" && view) ss("r-from");
    if (view === "recipe") { await renderDetail(root, id); return; }

    const tab = TABS.includes(view) ? view : "discover";
    const body = h("div.r-body");
    fill(root,
      h("div.r-head", h("h1", t("Kitchen")),
        h("button.primary", { onclick: () => newRecipe() }, icon("plus"), t("New recipe"))),
      h("div.tabs.r-tabs", TABS.map((k) =>
        h(`button${k === tab ? ".active" : ""}`, { onclick: () => go(`#/recipes/${k}`) }, TAB_LABEL()[k]))),
      body);
    ({ discover: renderDiscover, week: renderWeek, mine: renderMine, fridge: renderFridge, shopping: renderShopping, today: renderToday })[tab](body);
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
  const restore = !!sg("r-from") && Date.now() - (+sg("r-pop") || 0) < 2000;
  ss("r-pop");
  if (!restore) { ss("r-q"); ss("r-from"); }
  const input = h("input", { type: "search", placeholder: t("Search or paste a link…"), enterkeyhint: "search",
                             value: restore ? sg("r-q") || "" : "" });
  const clear = h("button.r-clear.icon.ghost", { type: "button", "aria-label": t("Clear"), onclick: () => {
    input.value = ""; ss("r-q"); ss("r-from"); input.dispatchEvent(new Event("input")); intro(out, input, submit); input.focus();
  } }, icon("x"));
  const out = h("div");
  const btn = h("button.primary", { type: "submit" });
  const label = () => { clear.hidden = !input.value; fill(btn, icon(isUrl(input.value) ? "download" : "search"), isUrl(input.value) ? t("Import") : t("Search")); };
  label();
  input.addEventListener("input", label);
  const submit = async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.blur();
    ss("r-q", q);
    if (isUrl(q)) return importRecipe({ url: q }, btn);
    fill(out, loading());
    try { showResults(out, await P(`search?q=${enc(q)}`), q); }
    catch (err) { fill(out, h("div.empty", err.message)); }
  };
  if (sg("r-opencook")) { ss("r-opencook"); renderCook(el); return; }
  fill(el, cookCard(el), h("form.r-search", { onsubmit: submit }, h("div.r-input", icon("search"), input, clear), btn), out);
  if (restore && input.value.trim() && !isUrl(input.value)) submit(new Event("submit"));
  else intro(out, input, submit);
}

const NOW_TITLE = () => ({ dinner: t("Ideas for tonight"), lunch: t("Ideas for lunch"), breakfast: t("Ideas for breakfast"),
  snack: t("Ideas for a snack"), quick: t("Something quick") });

function intro(out, input, submit) {
  const ideas = [t("pasta"), t("risotto"), t("chicken"), t("vegetables"), t("desserts"), t("soups")];
  const recent = h("div");
  const now = h("div.r-now");
  const first = h("div");
  let n = 0;
  const loadNow = () => P(`suggest?n=${n}`).then((r) => {
    if (!r.items?.length) return fill(now);
    fill(now, h("div.r-nowhead", h("h2.r-sec", NOW_TITLE()[r.theme] || `${t("Ideas for now")}${r.label ? ` · ${r.label}` : ""}`),
      h("button.icon.ghost", { type: "button", "aria-label": t("Other ideas"), title: t("Other ideas"), onclick: () => { n++; loadNow(); } }, icon("refresh"))),
      h("div.r-now-row", r.items.map((x) => {
        const c = h("div.r-mini", { title: x.title, onclick: () => (x.recipe_id ? go(`#/recipes/recipe/${x.recipe_id}`)
          : (ss("r-from", "1"), importRecipe(x.mealdb ? { mealdb: x.mealdb } : { url: x.url }, c))) },
          h("div.img", x.image ? h("img", { src: x.image, loading: "lazy", alt: "" }) : h("div.ph", icon("utensils"))),
          h("div.t", x.title), h("div.y", x.source));
        return c;
      })));
  }).catch(() => fill(now));
  loadNow();
  const showIntro = () => fill(first, h("div.r-empty.r-intro", h("div.r-empty-ic", icon("utensils")),
    h("p", t("Search for a recipe or paste a link: we save it in the app.")),
    h("div.r-ideas", ideas.map((w) => h("button.chipbtn", { onclick: () => { input.value = w; input.dispatchEvent(new Event("input")); submit(new Event("submit")); } }, w)))));
  fill(out, now, recent, first);
  P("recipes").then((items) => {
    if (!items.length) return showIntro();
    items = [...items].sort((a, b) => (b.updated || 0) - (a.updated || 0) || b.id - a.id).slice(0, 6);
    fill(recent, h("h2.r-sec", t("Your latest recipes")),
      h("div.r-grid", items.map((r) => card({ ...r, meta: cardMeta(r) }, () => go(`#/recipes/recipe/${r.id}`)))));
  }).catch(showIntro);
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
        () => { ss("r-from", "1"); importRecipe(r.mealdb ? { mealdb: r.mealdb } : { url: r.url }, c); },
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
      const taken = (await P("shopping")).filter((i) => i.checked).map((i) => i.name);
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
        const c = card({ ...r, meta: cardMeta(r) },
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

  const mins = rec.total_min || (rec.prep_min || 0) + (rec.cook_min || 0);
  const metaLine = h("div.r-meta",
    [mins ? fmtMin(mins) : "", rec.kcal ? `${rec.kcal_est ? "~" : ""}${rec.kcal} ${t("kcal per serving")}` : ""].filter(Boolean).join(" · "),
    rec.source_url && [mins || rec.kcal ? " · " : "", h("a", { href: rec.source_url, target: "_blank", rel: "noopener" }, (rec.site || "").replace(/^(www|ricette)\./, "") || t("Source"))],
    rec.edited && h("span.chip.warn", t("edited")));

  let target = rec.servings;
  const val = h("span.val", fmt.num(target, 1));
  const apply = debounce(() => { ctx.servings = target; act(async () => { await ctx.reload(); ctx.paint(); }); }, 350);
  const step = (d) => { target = Math.max(1, Math.round(target) + d); val.textContent = fmt.num(target, 1); apply(); };
  const sv = h("div.r-portions",
    h("span.lbl", t("Servings")),
    h("div.stepper.big",
      h("button.icon", { onclick: () => step(-1), "aria-label": "-" }, icon("minus")), val,
      h("button.icon", { onclick: () => step(1), "aria-label": "+" }, icon("plus"))),
    rec.base_servings !== rec.servings ? h("span.small.muted.r-orig", t("original recipe: {n}", { n: fmt.num(rec.base_servings, 1) })) : null);

  const ingList = h("div.r-ings", rec.ingredients.map((i, idx) => {
    if (i.heading) return h("div.r-sub", i.heading);
    return h("div.r-ing", h("button.txt.r-tap", { title: t("Edit ingredient"), onclick: () => quickIngredient(rec, idx, ctx) }, i.text));
  }));

  const steps = [];
  let n = 0;
  rec.steps.forEach((s, idx) => {
    if (s.startsWith("# ")) { steps.push(h("div.r-sub", s.slice(2))); return; }
    steps.push(h("div.r-step", h("span.n", ++n), h("button.r-tap", { title: t("Edit step"), onclick: () => quickStep(rec, idx, ctx) }, s)));
  });

  const notes = h("textarea", { rows: 3, placeholder: t("Your notes: tweaks, tips, family verdict…"), value: rec.notes });
  notes.addEventListener("change", () => act(async () => { await P(`recipe/${id}`, { method: "PUT", body: { notes: notes.value } }); rec.notes = notes.value; }));

  const tagInput = h("input", { placeholder: t("Tags (comma separated)"), value: rec.tags.join(", ") });
  tagInput.addEventListener("change", () => upd({ tags: tagInput.value }));

  fill(view,
    h("div.r-hero", rec.image ? h("img", { src: rec.image, alt: "" }) : h("div.ph", icon("utensils")),
      h("button.icon.back", { onclick: () => (sg("r-from") ? (ss("r-pop", String(Date.now())), history.back()) : go("#/recipes/mine")), "aria-label": t("Back") }, icon("left"))),
    h("div.r-titlebar", h("h1", rec.title), heart),
    metaLine,
    h("div.r-actions",
      h("button.primary", { onclick: () => addToMenu(rec) }, icon("calendar"), t("Add to the menu")),
      rec.steps.some((s) => !s.startsWith("# ")) ? h("button.r-cookbtn", { onclick: () => cookMode(rec) }, icon("utensils"), t("Cook!")) : null,
      h("button.icon.r-more", { "aria-label": t("More"), title: t("More"), onclick: () => moreMenu(view, rec, ctx) }, icon("more"))),
    sv,
    h("div.r-cols",
      h("div.card.stack", { style: { gap: "10px" } },
        h("h2", t("Ingredients")),
        ingList,
        h("button", { onclick: (e) => act(async () => {
          await P("shopping", { body: { recipe_id: rec.id, servings: rec.servings } }); toast(t("Added to the shopping list"));
        }, e.currentTarget) }, icon("cart"), t("Add all to the shopping list"))),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("Method")), steps.length ? steps : h("div.muted", "–"))),
    h("div.r-rate", h("span.lbl", t("Your rating")), stars),
    h("div.card.stack", { style: { gap: "8px" } }, h("h2", t("Personal notes")), notes,
      h("label.field", t("Tags (comma separated)").split(" (")[0], tagInput)));
}

/** The "⋯" sheet on the recipe page: everything that is not the main path. */
function moreMenu(view, rec, ctx) {
  const { id } = ctx;
  const sh = sheet(rec.title, []);
  const item = (ic, label, fn, cls = "") => h(`button.lg${cls}`, { onclick: (e) => { fn(e); } }, icon(ic), label);
  const closeThen = (fn) => (e) => { sh.close(); fn(e); };
  const adapt = adaptButton(rec, ctx, () => sh.close());
  sh.el.replaceChildren(...[
    item("edit", t("Edit"), closeThen(() => editMode(view, rec, ctx))),
    adapt,
    item("utensils", t("Log a portion"), closeThen(() => logPortion(rec))),
    hasPlugin("sync") ? item("users", t("Share with friends"), (e) => act(async () => {
      sh.close();
      const r = await api("p/sync/recipes/share", { body: { recipe_id: rec.id } });
      const el = h("div.toast", `${t("Recipe shared.")} `, h("a", { href: `#/social/recipe/${encodeURIComponent(r.slug)}` }, t("View")));
      document.getElementById("toasts").append(el);
      setTimeout(() => el.remove(), 7000);
    }, e.currentTarget)) : null,
    rec.has_original && rec.edited ? item("refresh", t("Reset to original"), closeThen(() => confirm(t("Go back to the original version? Your edits to title, ingredients and method will be lost.")) && act(async () => {
      await P(`recipe/${id}/reset`, { body: {} }); mySessionServings.delete(id); ctx.servings = status.default_servings || 0; await ctx.reload(); ctx.paint();
    }))) : null,
    rec.source_url ? h("a.r-menulink", { href: rec.source_url, target: "_blank", rel: "noopener", onclick: () => sh.close() }, icon("link"), `${t("Source")}: ${rec.site || ""}`) : null,
    item("trash", t("Delete"), closeThen(() => confirm(t("Delete this recipe from your hub?")) && act(async () => {
      await P(`recipe/${id}`, { method: "DELETE" }); toast(t("Recipe deleted")); go("#/recipes/mine");
    })), ".danger"),
  ].filter(Boolean));
}

// ---------------------------------------------------------------- cooking mode

/** Full-screen, one step per screen, big buttons + swipe, screen kept awake. */
function cookMode(rec) {
  const steps = [];
  let head = "";
  rec.steps.forEach((s) => { if (s.startsWith("# ")) head = s.slice(2); else steps.push({ text: s, head }); });
  if (!steps.length) return;
  // ingredients mentioned in a step: any word (>= 4 letters) of the stored name, matched by stem
  const stem = (w) => (w.length > 4 ? w.slice(0, -1) : w);
  const keys = rec.ingredients.map((i, idx) => {
    if (i.heading) return null;
    const words = norm(rec.rows?.[idx]?.name || "").split(/[^a-z]+/).filter((w) => w.length >= 4).map(stem);
    return words.length ? words : null;
  });
  const related = (text) => {
    const nt = norm(text);
    return rec.ingredients.filter((i, idx) => keys[idx]?.some((w) => nt.includes(w))).slice(0, 6);
  };
  let at = 0, all = false, lock = null;
  const body = h("div.r-cm-body"), count = h("span.r-cm-count");
  const prev = h("button.r-cm-nav", { "aria-label": t("Previous step"), onclick: () => go_(-1) }, icon("left"));
  const next = h("button.r-cm-nav.primary", { "aria-label": t("Next step"), onclick: () => (at === steps.length - 1 ? close() : go_(1)) });
  const ingBtn = h("button.r-cm-ingbtn", { onclick: () => { all = !all; paint(); } });
  const root = h("div.r-cm", { role: "dialog" },
    h("div.r-cm-top", h("button.icon.ghost", { "aria-label": t("Close"), onclick: () => close() }, icon("x")), count, ingBtn), body,
    h("div.r-cm-bar", prev, next));
  function paint() {
    const s = steps[at];
    count.textContent = `${at + 1} / ${steps.length}`;
    fill(ingBtn, icon("eye"), all ? t("Back to the step") : t("Ingredients"));
    prev.disabled = at === 0;
    fill(next, at === steps.length - 1 ? [icon("check"), t("Done")] : icon("right"));
    if (all) return fill(body, h("h2", `${t("Ingredients")} · ${t("{n} people", { n: fmt.num(rec.servings, 1) })}`),
      h("div.r-cm-ings", rec.ingredients.map((i) => (i.heading ? h("div.r-sub", i.heading) : h("div.r-cm-ing", i.text)))));
    const rel = related(s.text);
    fill(body, s.head ? h("div.r-cm-head", s.head) : null, h("p.r-cm-step", s.text),
      rel.length ? h("div.r-cm-rel", rel.map((i) => h("span.r-cm-chip", i.text))) : null);
    body.scrollTop = 0;
  }
  function go_(d) { at = Math.min(steps.length - 1, Math.max(0, at + d)); paint(); }
  let sx = 0, sy = 0;
  root.addEventListener("touchstart", (e) => { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }, { passive: true });
  root.addEventListener("touchend", (e) => {
    const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    if (!all && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) go_(dx < 0 ? 1 : -1);
  }, { passive: true });
  const key = (e) => { if (e.key === "ArrowRight") go_(1); else if (e.key === "ArrowLeft") go_(-1); else if (e.key === "Escape") close(); };
  const wake = async () => { try { lock = await navigator.wakeLock?.request("screen"); } catch { lock = null; } };
  const vis = () => { if (document.visibilityState === "visible") wake(); };
  function close() {
    document.removeEventListener("keydown", key); document.removeEventListener("visibilitychange", vis);
    window.removeEventListener("hashchange", close);
    try { lock?.release(); } catch {}
    root.remove();
  }
  document.addEventListener("keydown", key); document.addEventListener("visibilitychange", vis);
  window.addEventListener("hashchange", close);
  document.body.append(root);
  wake(); paint();
}

// ---------------------------------------------------------------- editing helpers

const UNITS = () => [["", "–"], ["g", "g"], ["kg", "kg"], ["ml", "ml"], ["cl", "cl"], ["dl", "dl"], ["l", "l"],
  ["tbsp", t("tablespoon")], ["tsp", t("teaspoon")], ["cup", t("cup")], ["glass", t("glass")], ["clove", t("clove")],
  ["leaf", t("leaf")], ["pinch", t("pinch")], ["handful", t("handful")], ["pack", t("pack")], ["slice", t("slice")],
  ["bunch", t("bunch")], ["sprig", t("sprig")], ["can", t("can")], ["oz", "oz"], ["lb", "lb"], ["qb", "q.b."]];

const qtyText = (q, q2) => {
  if (q == null) return "";
  const f = (x) => String(Math.round(x * 100) / 100).replace(".", ",");
  return q2 ? `${f(q)}-${f(q2)}` : f(q);
};
function parseQty(s) {
  const one = (x) => {
    x = x.trim().replace(",", ".");
    const m = x.match(/^(?:(\d+)\s+)?(\d+)\/(\d+)$/);
    if (m) return (+m[1] || 0) + (+m[2]) / (+m[3] || 1);
    const n = parseFloat(x);
    return x === "" || isNaN(n) ? null : n;
  };
  const m = String(s).match(/^\s*([\d.,/ ]+?)\s*[-–]\s*([\d.,/ ]+)\s*$/);
  if (m) { const a = one(m[1]), b = one(m[2]); return a == null ? [null, null] : [a, b]; }
  return [one(String(s)), null];
}
function qtyStep(unit, q) {
  if (unit === "g" || unit === "ml") return q >= 100 ? 50 : q >= 10 ? 10 : 1;
  if (unit === "kg" || unit === "l" || unit === "cup" || unit === "glass") return 0.25;
  if (unit === "tbsp" || unit === "tsp") return 0.5;
  if (unit === "cl" || unit === "dl") return q >= 10 ? 5 : 1;
  return 1;
}
const unitValue = (row) => row.taste && row.qty == null ? "qb" : row.unit || "";

/** Quantity (stepper + text, Italian comma accepted) + unit select for one row; edits `row` in place. */
function qtyControls(row, onChange) {
  const input = h("input.r-qty", { type: "text", inputmode: "decimal", value: qtyText(row.qty, row.qty2), placeholder: "–", "aria-label": t("Quantity") });
  const sel = h("select.r-unit", { "aria-label": t("Unit") });
  const opts = UNITS();
  if (row.unit && !opts.some(([v]) => v === row.unit)) opts.push([row.unit, row.unit]); // unknown unit from the source
  for (const [v, l] of opts) sel.append(h("option", { value: v }, l));
  sel.value = unitValue(row);
  const sync = () => {
    const [q, q2] = parseQty(input.value);
    row.qty = q; row.qty2 = q2;
    if (sel.value === "qb") { row.taste = true; row.unit = ""; row.qty = null; row.qty2 = null; }
    else { row.taste = false; row.unit = sel.value; }
    onChange?.();
  };
  input.addEventListener("input", sync);
  sel.addEventListener("change", () => { if (sel.value === "qb") input.value = ""; sync(); });
  const step = (d) => {
    const q = row.qty || 0;
    const s = qtyStep(row.unit, d < 0 ? q - 0.001 : q);
    const next = Math.max(0, Math.round((q + d * s) * 100) / 100);
    row.qty = next || null; row.qty2 = null;
    if (row.taste) { row.taste = false; sel.value = row.unit || ""; }
    input.value = qtyText(row.qty);
    onChange?.();
  };
  const stepper = h("div.r-qstep",
    h("button.icon", { type: "button", onclick: () => step(-1), "aria-label": "-" }, icon("minus")), input,
    h("button.icon", { type: "button", onclick: () => step(1), "aria-label": "+" }, icon("plus")));
  return { stepper, sel };
}

const grow = (ta) => { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight + 2}px`; };
const growSoon = (ta) => requestAnimationFrame(() => grow(ta));
const pasteParts = (text) => {
  const paras = text.replace(/\r/g, "").split(/\n\s*\n/).map((x) => x.trim()).filter(Boolean);
  if (paras.length > 1) return paras.map((x) => x.replace(/\s*\n\s*/g, " "));
  return text.split("\n").map((x) => x.trim()).filter(Boolean);
};
const moveItem = (arr, i, d) => { const j = i + d; if (j < 0 || j >= arr.length) return; [arr[i], arr[j]] = [arr[j], arr[i]]; };
const rowBtns = (arr, i, redraw) => [
  h("button.icon.r-mv", { type: "button", "aria-label": t("Move up"), onclick: () => { moveItem(arr, i, -1); redraw(); } }, icon("up")),
  h("button.icon.r-mv.down", { type: "button", "aria-label": t("Move down"), onclick: () => { moveItem(arr, i, 1); redraw(); } }, icon("up")),
  h("button.icon.r-mv.danger", { type: "button", "aria-label": t("Remove"), onclick: () => { arr.splice(i, 1); redraw(); } }, icon("x")),
];

function ingredientEditor(rows, onChange) {
  const box = h("div.r-rows");
  let focusNext = -1;
  const draw = () => {
    fill(box, rows.map((row, i) => {
      if (row.heading != null) {
        const inp = h("input", { value: row.heading, placeholder: t("Section title"), "aria-label": t("Section title") });
        inp.addEventListener("input", () => { row.heading = inp.value; onChange(); });
        return h("div.r-row.head", inp, rowBtns(rows, i, redraw));
      }
      const { stepper, sel } = qtyControls(row, onChange);
      const name = h("input.r-name", { value: row.name || "", placeholder: t("Ingredient"), "aria-label": t("Ingredient"), autocapitalize: "none" });
      name.addEventListener("input", () => { row.name = name.value; onChange(); });
      name.addEventListener("paste", (e) => {
        const text = e.clipboardData?.getData("text") || "";
        if (!/\n/.test(text.trim())) return;
        e.preventDefault();
        act(async () => {
          const r = await P("edit/parse", { body: { text } });
          const at = row.name ? i + 1 : i;
          rows.splice(at, row.name ? 0 : 1, ...r.rows.map((x) => (x.heading != null ? x : { ...x, raw: "" })));
          onChange(); redraw();
        });
      });
      const note = h("input.r-note", { value: row.note || "", placeholder: t("Note (optional)"), "aria-label": t("Note (optional)") });
      note.addEventListener("input", () => { row.note = note.value; onChange(); });
      if (i === focusNext) setTimeout(() => name.focus(), 0);
      return h("div.r-row", h("div.r-rq", stepper, sel), h("div.r-rn", name, rowBtns(rows, i, redraw)), note);
    }));
    focusNext = -1;
  };
  const redraw = () => { draw(); onChange(); };
  draw();
  const add = () => { rows.push({ qty: null, qty2: null, unit: "", name: "", note: "", taste: false }); focusNext = rows.length - 1; redraw(); };
  const addHead = () => { rows.push({ heading: "" }); redraw(); };
  return { el: h("div.stack", { style: { gap: "10px" } }, box,
    h("div.row.wrap", { style: { gap: "8px" } },
      h("button", { type: "button", onclick: add }, icon("plus"), t("Add ingredient")),
      h("button", { type: "button", onclick: addHead }, icon("plus"), t("Add section")))), redraw };
}

function stepsEditor(items, onChange) {
  const box = h("div.r-rows");
  let focusNext = -1;
  const draw = () => {
    let n = 0;
    fill(box, items.map((it, i) => {
      const ta = h("textarea.r-stepta", { rows: 1, value: it.text, placeholder: it.heading ? t("Section title") : t("Describe this step…") });
      ta.addEventListener("input", () => { it.text = ta.value; grow(ta); onChange(); });
      if (!it.heading) ta.addEventListener("paste", (e) => {
        const text = e.clipboardData?.getData("text") || "";
        const parts = pasteParts(text);
        if (parts.length < 2) return;
        e.preventDefault();
        const a = ta.selectionStart, b = ta.selectionEnd;
        it.text = ta.value.slice(0, a) + parts[0] + ta.value.slice(b);
        items.splice(i + 1, 0, ...parts.slice(1).map((x) => ({ text: x })));
        redraw();
      });
      growSoon(ta);
      if (i === focusNext) setTimeout(() => ta.focus(), 0);
      if (it.heading) return h("div.r-row.head", ta, rowBtns(items, i, redraw));
      return h("div.r-stepcard", h("span.n", ++n), ta, h("div.r-stepbtns", rowBtns(items, i, redraw)));
    }));
    focusNext = -1;
  };
  const redraw = () => { draw(); onChange(); };
  draw();
  const add = () => { items.push({ text: "" }); focusNext = items.length - 1; redraw(); };
  return h("div.stack", { style: { gap: "10px" } }, box, h("button", { type: "button", onclick: add }, icon("plus"), t("Add step")));
}

const stepItems = (steps) => steps.map((s) => (s.startsWith("# ") ? { text: s.slice(2), heading: true } : { text: s }));
const stepLines = (items) => items.filter((x) => x.text.trim()).map((x) => (x.heading ? `# ${x.text.trim()}` : x.text.trim()));

/** Rescale rows to `n` servings on the server (same rounding rules as the Python tests). */
const rescaleRows = (rows, from, to) => P("edit/rescale", { body: { rows, from, to } }).then((r) => r.rows);

function editMode(view, rec, ctx) {
  const rows = JSON.parse(JSON.stringify(rec.rows));
  const items = stepItems(rec.steps);
  const title = h("input", { value: rec.title });
  const sv = h("input", { type: "number", min: 1, step: 1, value: rec.base_servings, inputmode: "decimal" });
  const orig = rec.kcal_est ? "" : String(rec.kcal || "");
  const kcal = h("input", { type: "number", min: 0, value: orig, placeholder: rec.kcal_est ? `~${rec.kcal}` : "" });
  const snap = () => JSON.stringify([title.value, sv.value, kcal.value, rows, items]);
  const start = snap();
  const dirty = () => snap() !== start;
  const ie = ingredientEditor(rows, () => {});
  const se = stepsEditor(items, () => {});

  const target = h("input", { type: "number", min: 1, step: 1, value: status.default_servings || 2, inputmode: "numeric", "aria-label": t("Servings") });
  const adapt = h("button", { type: "button", onclick: (e) => act(async () => {
    const to = Math.max(1, Math.round(+target.value || 0));
    const from = +sv.value || rec.base_servings;
    if (to === from) return;
    const out = await rescaleRows(rows, from, to);
    rows.splice(0, rows.length, ...out); sv.value = to;
    ie.redraw();
    toast(t("Quantities adapted to {n} servings", { n: to }));
  }, e.currentTarget) }, icon("refresh"), t("Adapt quantities"));

  let off = () => {};
  const leave = () => { off(); ctx.paint(); };
  const guardClick = (e) => {
    if (!view.isConnected) { off(); return; }
    const a = e.target.closest?.("a[href^='#']");
    if (a && dirty() && !confirm(t("Leave without saving?"))) { e.preventDefault(); e.stopPropagation(); }
  };
  const guardUnload = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  document.addEventListener("click", guardClick, true);
  window.addEventListener("beforeunload", guardUnload);
  off = () => { document.removeEventListener("click", guardClick, true); window.removeEventListener("beforeunload", guardUnload); };

  const bar = h("div.r-editbar",
    h("button", { onclick: () => { if (!dirty() || confirm(t("Leave without saving?"))) leave(); } }, t("Cancel")),
    h("button.primary", { onclick: (e) => act(async () => {
      const patch = { title: title.value, servings: +sv.value, ingredients: rows, steps: stepLines(items) };
      if (kcal.value !== orig) patch.kcal = kcal.value === "" ? null : +kcal.value;
      ctx.servings = +sv.value === rec.base_servings ? ctx.servings : +sv.value;
      await ctx.reload(patch); off(); ctx.paint(); toast(t("Saved"));
    }, e.currentTarget) }, icon("check"), t("Save")));
  fill(view, bar, h("h1", t("Edit")),
    h("div.card.stack", h("label.field", t("Title"), title), h("label.field", t("Servings in the original recipe"), sv),
      h("div.r-adapt", h("span.lbl", t("Adapt to")), target, h("span", t("servings")), adapt),
      h("p.small.muted", { style: { margin: 0 } }, t("Rescales every quantity with sensible rounding. \"Reset to original\" undoes it."))),
    h("div.card.stack", h("h2", t("Ingredients")), ie.el),
    h("div.card.stack", h("h2", t("Method")), se),
    h("div.card.stack", h("label.field", t("Calories per serving (empty = automatic)"), kcal)));
}

// Quick edits from the recipe page: tap an ingredient or a step.
function quickIngredient(rec, idx, ctx) {
  const stored = rec.rows[idx];
  const factor = rec.servings / rec.base_servings || 1;
  const i = rec.ingredients[idx];
  // The sheet shows what the page shows (scaled); an untouched quantity keeps its stored value.
  const [shownQ] = parseQty(i.text.match(/^[\d.,/½¼¾⅓⅔ -]+/)?.[0]?.replace(/[½¼¾⅓⅔]/g, (c) => ({ "½": ".5", "¼": ".25", "¾": ".75", "⅓": ".33", "⅔": ".67" }[c])) ?? "");
  const row = { ...stored };
  if (stored.qty != null && factor !== 1) { row.qty = shownQ ?? stored.qty * factor; row.qty2 = null; }
  const touched = { q: false };
  const { stepper, sel } = qtyControls(row, () => { touched.q = true; });
  const name = h("input", { value: row.name || "", "aria-label": t("Ingredient") });
  const note = h("input", { value: row.note || "", placeholder: t("Note (optional)") });
  const repl = h("input", { placeholder: t("Substitute with…"), "aria-label": t("Substitute with…") });
  const save = async (r) => {
    const lines = rec.ingredient_lines.map((x, k) => (k === idx ? r : x)).filter((x) => x != null);
    await ctx.reload({ ingredients: lines }); ctx.paint(); s.close(); toast(t("Saved"));
  };
  const build = (nameVal) => {
    const out = { ...stored, name: nameVal, note: note.value, unit: row.unit, taste: row.taste };
    if (touched.q || row.taste !== stored.taste || row.unit !== stored.unit) {
      out.qty = row.qty != null && factor !== 1 ? Math.round((row.qty / factor) * 1000) / 1000 : row.qty;
      out.qty2 = null;
    }
    return out;
  };
  const s = sheet(t("Edit ingredient"), [
    h("div.small.muted", factor !== 1 ? t("Quantities for {n} servings", { n: fmt.num(rec.servings, 1) }) : ""),
    i.subs?.length ? h("div.r-subs", h("b", `${t("Substitutions")}: `), i.subs.join(" · ")) : null,
    h("button", { onclick: (e) => act(async () => {
      await P("shopping", { body: { name: i.text } }); s.close(); toast(t("Added to the shopping list"));
    }, e.currentTarget) }, icon("cart"), t("Add to shopping list")),
    h("div.r-rq", stepper, sel),
    h("label.field", t("Ingredient"), name), h("label.field", t("Note (optional)"), note),
    h("button.primary.lg", { onclick: (e) => act(() => save(build(name.value)), e.currentTarget) }, icon("check"), t("Save")),
    h("label.field", t("Substitute with…"), repl),
    h("button", { onclick: (e) => repl.value.trim() ? act(() => save(build(repl.value.trim())), e.currentTarget) : repl.focus() }, icon("refresh"), t("Substitute")),
    h("button.danger", { onclick: (e) => act(() => save(null), e.currentTarget) }, icon("trash"), t("Remove from recipe"))]);
}

function quickStep(rec, idx, ctx) {
  const ta = h("textarea", { rows: 5, value: rec.steps[idx] });
  const save = async (text) => {
    const steps = rec.steps.map((x, k) => (k === idx ? text : x)).filter((x) => x && x.trim());
    await ctx.reload({ steps }); ctx.paint(); s.close(); toast(t("Saved"));
  };
  const s = sheet(t("Edit step"), [ta,
    h("button.primary.lg", { onclick: (e) => act(() => save(ta.value.trim()), e.currentTarget) }, icon("check"), t("Save")),
    h("button.danger", { onclick: (e) => act(() => save(""), e.currentTarget) }, icon("trash"), t("Remove step"))]);
  setTimeout(() => { ta.focus(); grow(ta); }, 50);
}

/** "Adapt to N servings" on the recipe page: permanent rescale, "Reset to original" undoes it. */
function adaptButton(rec, ctx, before) {
  if (!rec.servings || Math.abs(rec.servings - rec.base_servings) < 1e-6) return null;
  const n = Math.round(rec.servings);
  return h("button.lg", { onclick: (e) => act(async () => {
    before?.();
    if (!confirm(t("Rewrite the recipe for {n} servings? You can undo with \"Reset to original\".", { n }))) return;
    const rows = await rescaleRows(rec.rows, rec.base_servings, n);
    ctx.servings = n;
    await ctx.reload({ servings: n, ingredients: rows }); ctx.paint(); toast(t("Saved"));
  }, e.currentTarget) }, icon("refresh"), t("Adapt recipe to {n} servings", { n }));
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

// Aisles for the shopping list: simple Italian keyword rules, first match wins. A trailing "$" = whole word.
const AISLES = () => [
  [t("Dairy and eggs"), ["latte", "uov", "burro", "formaggi", "parmigian", "grana", "mozzarell", "ricott", "yogurt", "panna", "mascarpone",
    "pecorino", "gorgonzola", "provol", "scamorz", "stracchino", "feta", "philadelphia"]],
  [t("Meat and fish"), ["pollo", "manzo", "maial", "vitello", "salsicc", "prosciutt", "pancetta", "guanciale", "speck", "bacon", "tacchino",
    "agnello", "macinato", "salmone", "gamberi", "pesce", "merluzzo", "acciughe", "cozze", "vongole", "calamari", "polpo", "salame",
    "mortadella", "bresaola", "wurstel", "hamburger", "bistecca", "cotoletta", "arrosto", "branzino", "orata", "baccal"]],
  [t("Bread and pasta"), ["pane", "pasta", "spaghetti", "penne", "riso", "farina", "pangrattato", "gnocchi", "lasagn", "fusilli", "rigatoni",
    "tagliatelle", "pizza", "couscous", "tortilla", "grissini", "cracker", "biscott", "semola", "orzo", "farro", "fette biscottate"]],
  [t("Pantry"), ["olio", "sale$", "pepe$", "zucchero", "aceto", "passata", "pelati", "scatola", "lenticchie", "ceci", "fagioli", "brodo", "dado",
    "miele", "cioccolat", "cacao", "lievito", "vino", "spezie", "curry", "paprika", "origano", "noci", "mandorle", "pinoli", "conserva",
    "salsa", "senape", "maionese", "caffe", "marmellata", "capperi", "olive", "tonno", "zafferano", "cannella", "vaniglia"]],
  [t("Fruit and vegetables"), ["pomodor", "cipoll", "aglio", "carot", "zucchin", "patat", "melanzan", "peperon", "insalata", "lattuga", "spinac",
    "basilico", "prezzemolo", "limon", "mela$", "mele$", "banan", "fung", "sedano", "rucola", "fragol", "arancia", "arance", "zucca", "cavol",
    "broccol", "finocch", "pisell", "fagiolini", "porro", "porri", "scalogno", "rosmarino", "salvia", "timo", "menta", "verdur", "frutta",
    "avocado", "pera$", "pere$", "uva", "carciof", "asparag", "radicchio", "cetriol", "pesca", "pesche", "kiwi", "lime", "zenzero", "erba cipollina"]],
];
const AISLE_RX = {};
function aisleOf(text) {
  const x = norm(text);
  for (const [name, words] of AISLES()) {
    const rx = AISLE_RX[name] ||= new RegExp(`\\b(?:${words.map((w) => (w.endsWith("$") ? `${w.slice(0, -1)}\\b` : w)).join("|")})`);
    if (rx.test(x)) return name;
  }
  return t("Other");
}

async function renderShopping(el) {
  const list = h("div");
  const input = h("input", { placeholder: t("Add (e.g. milk)"), enterkeyhint: "done" });
  const addForm = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!input.value.trim()) return;
    act(async () => { const r = await P("shopping", { body: { name: input.value } }); input.value = ""; paint(r.items); }); } },
    h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("Add")));
  fill(el, h("h2.r-sec.r-first", t("Shopping list")), addForm, list);
  const call = (path, opts) => act(async () => paint((await P(path, opts)).items));
  const mon = mondayOf(localDay());
  let planned = 0;
  try { planned = (await P(`plan?week=${mon}`)).days.reduce((k, d) => k + (d.lunch?.recipe_id ? 1 : 0) + (d.dinner?.recipe_id ? 1 : 0), 0); } catch {}

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
    let openEl = open.length ? h("div.list", open.map(row)) : h("div.empty.r-allin", t("Everything taken!"));
    if (open.length > 8) {
      const groups = new Map([...AISLES().map(([n]) => n), t("Other")].map((n) => [n, []]));
      for (const i of open) groups.get(aisleOf(i.text)).push(i);
      openEl = [...groups].filter(([, g]) => g.length).map(([name, g]) => h("div",
        h("h2.r-sec.r-aisle", name), h("div.list", g.map(row))));
    }
    fill(list,
      items.length ? [
        openEl,
        done.length ? h("div.r-donehead", h("h2.r-sec", `${t("Taken")} (${done.length})`),
          h("button", { onclick: () => call("shopping", { method: "DELETE" }) }, icon("trash"), t("Clear taken"))) : null,
        done.length ? h("div.list", done.map(row)) : null,
        h("div.row", { style: { marginTop: "12px" } },
          h("button.danger", { onclick: () => confirm(t("Clear all") + "?") && call("shopping?all=1", { method: "DELETE" }) }, icon("trash"), t("Clear all")))]
        : emptyState("cart", t("Nothing to buy. Open a recipe and add its ingredients."),
          planned ? h("button.primary", { onclick: (e) => act(async () => {
            const r = await P("plan/shopping", { body: { week: mon } });
            if (!r.added) return toast(t("Nothing to add: plan some recipes first"));
            paint(await P("shopping"));
          }, e.currentTarget) }, icon("calendar"), t("Add from the week")) : null,
          h(`button${planned ? "" : ".primary"}`, { onclick: () => go("#/recipes/mine") }, icon("utensils"), t("Recipe book"))));
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
    const dishes = plan.days.reduce((k, d) => k + (d.lunch?.recipe_id ? 1 : 0) + (d.dinner?.recipe_id ? 1 : 0), 0);
    fill(actions,
      dishes ? h("button.primary.lg.w-shop", { onclick: (e) => act(async () => {
        const r = await P("plan/shopping", { body: { week: mon } });
        if (!r.added) return toast(t("Nothing to add: plan some recipes first"));
        toastLink(t("{n} ingredients on the list", { n: r.added }), t("Shopping list"), "#/recipes/shopping");
      }, e.currentTarget) }, icon("cart"), dishes === 1 ? t("Add the ingredients of 1 dish to the shopping list")
        : t("Add the ingredients of {n} dishes to the shopping list", { n: dishes })) : null,
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
      h("div.w-dayname", h("b", fmt.date(dateOf(d.date))), d.date === today ? h("span.chip.accent", t("Today")) : null,
        h("div.spacer"), d.lunch ? null : h("button.w-lunchadd", { onclick: () => pickDish(d.date, "lunch", load) }, `+ ${t("lunch").toLowerCase()}`)),
      ["lunch", "dinner"].map((m) => slotEl(d, m)))));
  }

  function slotEl(d, meal) {
    const s = d[meal];
    const lbl = h("span.w-meal", MEAL_LABEL()[meal]);
    if (!s && meal === "lunch") return null;
    if (!s) return h("div.w-slot", lbl, h("button.w-add", { onclick: () => pickDish(d.date, meal, load) }, icon("plus"), t("Add")));
    const img = s.image ? h("img", { src: s.image, alt: "", loading: "lazy" }) : h("div.ph", icon("utensils"));
    return h("div.w-slot", lbl, h("button.w-filled", { onclick: () => slotSheet(s, load) },
      h("span.w-img", img), h("span.w-title", s.title),
      s.servings !== (status.default_servings || 2) ? h("span.chip", people(s.servings)) : null));
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

// ---------------------------------------------------------------- fridge ("Frigo")

const PLACES = () => [["fridge", t("Fridge"), "🧊"], ["freezer", t("Freezer"), "❄️"], ["pantry", t("Pantry"), "🥫"]];
const QUICK = ["latte", "uova", "burro", "yogurt", "formaggio", "prosciutto", "verdure", "frutta", "pane", "carne", "pesce", "pasta"];
const dayDiff = (iso) => Math.round((dateOf(iso) - dateOf(localDay())) / 864e5);
const agoText = (iso) => { const n = -dayDiff(iso); return n <= 0 ? t("added today") : t("added {n} d ago", { n }); };

function expiryBadge(iso) {
  if (!iso) return null;
  const n = dayDiff(iso);
  const text = n < 0 ? t("Expired") : n === 0 ? t("Expires today") : n === 1 ? t("Expires tomorrow") : t("Expires {date}", { date: fmt.date(dateOf(iso), { weekday: false }) });
  return h(`span.f-exp${n < 0 ? ".bad" : n <= 2 ? ".warn" : ""}`, icon(n <= 2 ? "alert" : "calendar"), text);
}

function placeSeg(value, onChange) {
  const box = h("div.seg.f-seg");
  const paint = () => fill(box, PLACES().map(([k, label, em]) =>
    h(`button${k === value ? ".active" : ""}`, { type: "button", onclick: () => { value = k; paint(); onChange?.(k); } }, `${em} ${label}`)));
  paint();
  return { el: box, get value() { return value; } };
}

async function renderFridge(el) {
  let items = [], known = [], place = jsonKey("r-fplace", "fridge"), q = "";
  const input = h("input", { placeholder: t("What did you put in? (e.g. milk)"), enterkeyhint: "done", autocomplete: "off", autocapitalize: "none", maxlength: 60 });
  const qty = h("input", { placeholder: t("Quantity"), enterkeyhint: "done", autocomplete: "off", maxlength: 40 });
  const exp = h("input", { type: "date", "aria-label": t("Expires") });
  const seg = placeSeg(place, (k) => { try { sessionStorage.setItem("r-fplace", JSON.stringify(k)); } catch {} });
  const sugg = h("div.r-ideas.r-sugg"), quick = h("div.r-ideas.f-quick"), soon = h("div"), groups = h("div");
  const search = h("input", { type: "search", placeholder: t("Search the fridge…"), oninput: (e) => { q = norm(e.target.value); paintList(); } });

  const load = (r) => { items = r.items; paintList(); paintQuick(); };
  const add = () => {
    const name = input.value.trim();
    if (!name) return;
    act(async () => {
      load(await P("fridge", { body: { name, place: seg.value, qty: qty.value, expires: exp.value } }));
      input.value = qty.value = exp.value = ""; paintSugg(); input.focus();
    });
  };
  function paintSugg() {
    const v = norm(input.value);
    const have = new Set(items.map((i) => norm(i.name)));
    const list = v ? known.filter((k) => norm(k).includes(v) && !have.has(norm(k))).slice(0, 6) : [];
    fill(sugg, list.map((k) => h("button.chipbtn", { type: "button", onclick: () => { input.value = k; paintSugg(); (qty.focus(), 0); } }, icon("plus"), k)));
  }
  function paintQuick() {
    const have = new Set(items.map((i) => norm(i.name)));
    const list = QUICK.filter((k) => !have.has(norm(k)));
    fill(quick, list.map((k) => h("button.chipbtn", { type: "button", onclick: (e) => act(async () => {
      load(await P("fridge", { body: { name: k, place: seg.value } }));
    }, e.currentTarget) }, icon("plus"), k)));
  }
  input.addEventListener("input", paintSugg);
  const form = h("form.f-add", { onsubmit: (e) => { e.preventDefault(); add(); } },
    h("div.r-input", icon("plus"), input),
    seg.el,
    h("div.f-opts", qty, h("label.f-date", h("span", t("Expires")), exp)),
    h("button.primary", { type: "submit" }, icon("plus"), t("Add")));

  function row(i) {
    const where = PLACES().find(([k]) => k === i.place);
    const parts = [i.qty ? h("span.f-qty", i.qty) : null,
      i.expires && dayDiff(i.expires) <= 3 && where ? h("span.muted", `${where[2]} ${where[1]}`) : null,   // in "Expiring soon"
      h("span.muted", agoText(i.added)), expiryBadge(i.expires)];
    return h("div.item.f-row", { onclick: (e) => !e.target.closest("button") && editSheet(i) },
      h("div.f-main", h("div.f-name", i.name), h("div.f-meta", parts)),
      h("button.f-done", { type: "button", "aria-label": t("Used up"), onclick: () => finishSheet(i) }, icon("check"), t("Used up")),
      h("button.icon.ghost", { type: "button", "aria-label": t("Edit"), onclick: () => editSheet(i) }, icon("edit")));
  }
  function paintList() {
    const list = items.filter((i) => !q || norm(i.name).includes(q));
    const urgent = list.filter((i) => i.expires && dayDiff(i.expires) <= 3).sort((a, b) => a.expires.localeCompare(b.expires));
    fill(soon, urgent.length ? [h("h2.r-sec", `⏰ ${t("Expiring soon")} (${urgent.length})`), h("div.list.f-soon", urgent.map(row))] : null);
    const shown = new Set(urgent.map((i) => i.id));   // already listed under "Expiring soon": once is enough
    fill(groups, PLACES().map(([k, label, em]) => {
      const all = list.filter((i) => i.place === k);
      const g = all.filter((i) => !shown.has(i.id)).sort((a, b) => a.name.localeCompare(b.name));
      return g.length ? h("div", h("h2.r-sec", `${em} ${label} `, h("span.f-count", all.length)), h("div.list", g.map(row))) : null;
    }), !items.length ? emptyState("storage", t("The fridge is empty. Add what you have above, or tap a quick item."))
      : !list.length ? h("p.muted", t("Nothing found.")) : null);
  }

  function editSheet(i) {
    const name = h("input", { value: i.name, maxlength: 60 }), qy = h("input", { value: i.qty, maxlength: 40, placeholder: t("Quantity") });
    const d = h("input", { type: "date", value: i.expires || "" });
    const sp = placeSeg(i.place);
    const s = sheet(i.name, [
      h("label.field", t("Name"), name), sp.el,
      h("div.f-opts", h("label.field", t("Quantity"), qy), h("label.field", t("Expires"), d)),
      h("div.row", { style: { justifyContent: "space-between" } },
        h("button.danger", { type: "button", onclick: (e) => act(async () => { load(await P(`fridge/${i.id}`, { method: "DELETE" })); s.close(); }, e.currentTarget) }, icon("trash"), t("Delete")),
        h("button.primary", { type: "button", onclick: (e) => act(async () => {
          load(await P(`fridge/${i.id}`, { method: "PATCH", body: { name: name.value, place: sp.value, qty: qy.value, expires: d.value } }));
          s.close();
        }, e.currentTarget) }, icon("check"), t("Save")))]);
  }
  function finishSheet(i) {
    const go_ = (to_shopping) => (e) => act(async () => {
      load(await P(`fridge/${i.id}/finish`, { body: { to_shopping } }));
      s.close(); if (to_shopping) toast(t("Added to the shopping list"));
    }, e.currentTarget);
    const s = sheet(t("{name} used up", { name: i.name }), [
      h("button.primary.f-big", { type: "button", onclick: go_(true) }, icon("cart"), t("Used up: add to the shopping list")),
      h("button.f-big", { type: "button", onclick: go_(false) }, icon("check"), t("Used up"))]);
  }

  const cook = h("button.r-cookcard.f-cook", { type: "button", onclick: () => { ss("r-opencook", "1"); try { sessionStorage.setItem("r-cook-pantry", "true"); } catch {} go("#/recipes/discover"); } },
    h("span.r-cookic", icon("utensils")),
    h("span.r-cooktx", h("b", t("What can I cook with this?")), h("span", t("Recipes that use what is in the fridge, freezer and pantry"))),
    h("span.r-cookgo", icon("search")));
  const ticked = h("button", { type: "button", "aria-label": t("Add ticked shopping items"), onclick: (e) => act(async () => {
    const taken = (await P("shopping")).filter((i) => i.checked).map((i) => i.name);
    if (!taken.length) return toast(t("No ticked items in the shopping list"));
    for (const name of taken) await P("fridge", { body: { name, place: seg.value } });
    load(await P("fridge")); toast(t("Added to the fridge"));
  }, e.currentTarget) }, icon("cart"), t("Add ticked shopping items"));

  fill(el, cook, h("div.f-card", form, sugg, h("div.f-quicklab", t("Quick add")), quick),
    h("div.f-tools", h("div.r-input", icon("search"), search), ticked), soon, groups);
  fill(groups, loading());
  try { load(await P("fridge")); known = (await P("ingredients")).items; } catch (e) { fill(groups, h("div.empty", e.message)); }
}
