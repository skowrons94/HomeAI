// Recipes: discover/import, personal recipe book, shopping list, calorie log (hub/plugins/recipes).
// Routes: #/recipes[/discover|mine|shopping|today|recipe/<id>]
import { api, h, icon, toast, sheet, ensureCss, act, debounce, go, hasPlugin } from "../app.js";

const P = (path, opts) => api(`p/recipes/${path}`, opts);
const enc = encodeURIComponent;
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ""));
const loading = () => h("div.empty", h("div.spin"));

// ---------------------------------------------------------------- i18n (Italian first)
const TXT = {
  title: ["Ricette", "Recipes"], discover: ["Scopri", "Discover"], mine: ["Le mie ricette", "My recipes"],
  shopping: ["Spesa", "Shopping"], today: ["Oggi", "Today"],
  searchPh: ["Cerca o incolla un link…", "Search or paste a link…"],
  search: ["Cerca", "Search"], import: ["Importa", "Import"], create: ["Nuova ricetta", "New recipe"],
  searching: ["Ricerca in corso…", "Searching…"], nothing: ["Nessun risultato.", "Nothing found."],
  discoverHint: ["Cerca su GialloZafferano e TheMealDB, oppure incolla il link di una ricetta da qualsiasi sito: viene salvata sul tuo hub e funziona anche offline.",
                 "Search GialloZafferano and TheMealDB, or paste the link of a recipe from any site: it is saved on your hub and works offline."],
  saved: ["Salvata", "Saved"], importing: ["Importo…", "Importing…"], imported: ["Ricetta salvata", "Recipe saved"],
  favourites: ["Preferite", "Favourites"], filterPh: ["Filtra le tue ricette…", "Filter your recipes…"],
  noRecipes: ["Nessuna ricetta salvata. Cerca o importa la prima!", "No saved recipes yet. Search or import your first one!"],
  back: ["Indietro", "Back"], servings: ["Porzioni", "Servings"], ingredients: ["Ingredienti", "Ingredients"],
  steps: ["Preparazione", "Method"], notes: ["Note personali", "Personal notes"],
  notesPh: ["Le tue note: modifiche, trucchi, voto della famiglia…", "Your notes: tweaks, tips, family verdict…"],
  addAll: ["Aggiungi alla spesa", "Add to shopping list"], added: ["Aggiunti alla lista della spesa", "Added to the shopping list"],
  edit: ["Modifica", "Edit"], save: ["Salva", "Save"], cancel: ["Annulla", "Cancel"],
  reset: ["Ripristina originale", "Reset to original"], resetQ: ["Tornare alla versione originale? Le tue modifiche a titolo, ingredienti e preparazione andranno perse.", "Go back to the original version? Your edits to title, ingredients and method will be lost."],
  del: ["Elimina", "Delete"], delQ: ["Eliminare questa ricetta dal tuo hub?", "Delete this recipe from your hub?"],
  logPortion: ["Registra una porzione", "Log a portion"], portions: ["Porzioni mangiate", "Portions eaten"],
  logged: ["Aggiunto al diario di oggi", "Added to today's log"],
  kcalServing: ["kcal a porzione", "kcal per serving"], estimate: ["stima dagli ingredienti", "estimated from the ingredients"],
  noKcal: ["calorie non note", "calories unknown"], titleF: ["Titolo", "Title"],
  ingF: ["Ingredienti (uno per riga, '# Titolo' per un gruppo)", "Ingredients (one per line, '# Heading' for a group)"],
  stepF: ["Preparazione (un passaggio per riga)", "Method (one step per line)"],
  tagsF: ["Tag (separati da virgola)", "Tags (comma separated)"],
  kcalF: ["Calorie a porzione (vuoto = automatico)", "Calories per serving (empty = automatic)"],
  edited: ["modificata", "edited"], source: ["Fonte", "Source"], subs: ["Sostituzioni", "Substitutions"],
  addOne: ["Aggiungi alla spesa", "Add to shopping list"],
  shopPh: ["Aggiungi un articolo (es. 2 kg patate)…", "Add an item (e.g. 2 kg potatoes)…"], add: ["Aggiungi", "Add"],
  clearChecked: ["Rimuovi spuntati", "Clear checked"], clearAll: ["Svuota lista", "Clear all"],
  shopEmpty: ["La lista è vuota. Apri una ricetta e aggiungi gli ingredienti.", "Nothing to buy. Open a recipe and add its ingredients."],
  fromRecipes: ["da", "from"], goal: ["obiettivo", "goal"], eaten: ["mangiate", "eaten"], left: ["restanti", "left"], over: ["oltre", "over"],
  manual: ["Aggiunta manuale", "Manual entry"], name: ["Cosa hai mangiato", "What you ate"], kcal: ["kcal", "kcal"],
  fromBook: ["Da una tua ricetta", "From your recipes"], last7: ["Ultimi 7 giorni", "Last 7 days"],
  logEmpty: ["Niente registrato per questo giorno.", "Nothing logged for this day."], pick: ["Scegli una ricetta…", "Pick a recipe…"],
  new: ["Nuova", "New"], manualTitle: ["Nuova ricetta", "New recipe"], needTitle: ["Serve un titolo", "A title is required"],
  min: ["min", "min"], prep: ["preparazione", "prep"], cook: ["cottura", "cook"], offline: ["Non raggiungibile", "Unreachable"],
  share: ["Condividi con gli amici", "Share with friends"], shared: ["Ricetta condivisa.", "Recipe shared."],
  view: ["Vedi", "View"],
  srcErr: ["Alcune fonti non rispondono", "Some sources didn't answer"],
};
let LANG = 0;
const t = (k) => (TXT[k] || [k, k])[LANG];

const fmtMin = (m) => !m ? "" : m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60}` : ""}` : `${m} ${t("min")}`;
const kcalText = (r) => r.kcal ? `${r.kcal_est ? "~" : ""}${r.kcal} kcal` : "";
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const isUrl = (s) => /^https?:\/\/\S+$/i.test(s.trim());
const FLAG = { it: "🇮🇹", en: "🇬🇧" };

let status = { language: "it", goal: 2000 };

const TABS = ["discover", "mine", "shopping", "today"];

export default {
  async render(root, args) {
    ensureCss(`/static/pages/recipes.css?v=${window.CASA_V}`);
    try { status = await P("status"); } catch (e) { fill(root, h("div.empty", e.message)); return; }
    LANG = status.language === "en" ? 1 : 0;
    const [view, id] = args;
    if (view === "recipe") { await renderDetail(root, id); return; }

    const tab = TABS.includes(view) ? view : "discover";
    const body = h("div.r-body");
    fill(root,
      h("div.r-head", h("h1", t("title")),
        h("button.primary", { onclick: () => newRecipe() }, icon("plus"), t("create"))),
      h("div.tabs", TABS.map((k) =>
        h(`button${k === tab ? ".active" : ""}`, { onclick: () => go(`#/recipes/${k}`) }, t(k)))),
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
  const input = h("input", { type: "search", placeholder: t("searchPh"), enterkeyhint: "search",
                             value: sessionStorage.getItem("r-q") || "" });
  const out = h("div");
  const btn = h("button.primary", { type: "submit" });
  const label = () => fill(btn, icon(isUrl(input.value) ? "download" : "search"), isUrl(input.value) ? t("import") : t("search"));
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
  else fill(out, h("div.empty", t("discoverHint")));
}

function showResults(out, res, q) {
  const warn = Object.keys(res.errors || {}).length
    ? h("div.r-banner", "⚠️", h("div", `${t("srcErr")}: ${Object.entries(res.errors).map(([k, v]) => `${k} (${v})`).join(", ")}`)) : null;
  fill(out, warn, res.results.length
    ? h("div.r-grid", res.results.map((r) => {
      const c = card({ image: r.image, title: r.title, meta: `${FLAG[r.lang] || ""} ${r.subtitle || r.site}` },
        () => importRecipe(r.mealdb ? { mealdb: r.mealdb } : { url: r.url }, c),
        r.saved ? h("span.r-badge", icon("check")) : null);
      return c;
    }))
    : h("div.empty", `${t("nothing")} “${q}”`));
}

async function importRecipe(body, busyEl) {
  busyEl.classList.add("busy");
  if (busyEl.tagName === "BUTTON") busyEl.disabled = true;
  try {
    toast(t("importing"));
    const r = await P("import", { body });
    if (!r.existing) toast(t("imported"));
    go(`#/recipes/recipe/${r.id}`);
  } catch (e) { toast(e.message, true); }
  finally { busyEl.classList.remove("busy"); if (busyEl.tagName === "BUTTON") busyEl.disabled = false; }
}

async function newRecipe() {
  const title = h("input", { placeholder: t("titleF") });
  const ing = h("textarea", { rows: 6, placeholder: "200 g spaghetti\n2 uova" });
  const steps = h("textarea", { rows: 4 });
  const sv = h("input", { type: "number", min: 1, value: status.default_servings || 2 });
  const s = sheet(t("manualTitle"), [
    h("label.field", t("titleF"), title), h("label.field", t("servings"), sv),
    h("label.field", t("ingF"), ing), h("label.field", t("stepF"), steps),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      if (!title.value.trim()) return toast(t("needTitle"), true);
      const r = await P("recipes", { body: { title: title.value, servings: +sv.value, ingredients: ing.value, steps: steps.value } });
      s.close(); go(`#/recipes/recipe/${r.id}`);
    }, e.currentTarget) }, t("save"))]);
  title.focus();
}

// ---------------------------------------------------------------- my recipes

async function renderMine(el) {
  const state = { fav: false, tag: "", q: "" };
  const list = h("div"), filters = h("div.r-filters");
  const q = h("input", { type: "search", placeholder: t("filterPh") });
  fill(el, h("div.r-search", h("div.r-input", icon("search"), q)), filters, list);
  const tags = await P("tags");
  const paintFilters = () => fill(filters,
    h(`button.chipbtn${state.fav ? ".active" : ""}`, { onclick: () => { state.fav = !state.fav; refresh(); } }, icon("heart"), t("favourites")),
    tags.slice(0, 14).map((x) => h(`button.chipbtn${state.tag === x.tag ? ".active" : ""}`,
      { onclick: () => { state.tag = state.tag === x.tag ? "" : x.tag; refresh(); } }, x.tag)));
  async function refresh() {
    paintFilters();
    const items = await P(`recipes?fav=${state.fav ? 1 : 0}&tag=${enc(state.tag)}&q=${enc(state.q)}`);
    fill(list, items.length
      ? h("div.r-grid", items.map((r) => card({ ...r, meta: [kcalText(r), fmtMin(r.total_min)].filter(Boolean).join(" · ") || r.site },
        () => go(`#/recipes/recipe/${r.id}`),
        [r.fav && h("span.r-badge.fav", icon("heart")), r.edited && h("span.r-badge.ed", icon("edit"))])))
      : h("div.empty", state.fav || state.tag || state.q ? t("nothing") : t("noRecipes")));
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

  const heart = h(`button.icon.heart${rec.fav ? ".on" : ""}`, { title: t("favourites"), onclick: () => upd({ fav: !rec.fav }) }, icon("heart"));
  const stars = h("div.stars", [1, 2, 3, 4, 5].map((n) =>
    h(`button.star${n <= rec.rating ? ".on" : ""}`, { onclick: () => upd({ rating: rec.rating === n ? 0 : n }), "aria-label": `${n}` }, icon("star"))));

  const meta = [
    rec.prep_min && `${t("prep")} ${fmtMin(rec.prep_min)}`, rec.cook_min && `${t("cook")} ${fmtMin(rec.cook_min)}`,
    !rec.prep_min && !rec.cook_min && rec.total_min && fmtMin(rec.total_min),
  ].filter(Boolean).join(" · ");

  const kcalChip = rec.kcal
    ? h("span.chip.accent", { title: rec.kcal_est ? t("estimate") : "" }, `${rec.kcal_est ? "~" : ""}${rec.kcal} ${t("kcalServing")}`)
    : h("span.chip", t("noKcal"));

  let target = rec.servings;
  const val = h("span.val", target % 1 ? target.toFixed(1) : target);
  const apply = debounce(() => { ctx.servings = target; act(async () => { await ctx.reload(); ctx.paint(); }); }, 350);
  const step = (d) => { target = Math.max(1, Math.round(target) + d); val.textContent = target; apply(); };
  const sv = h("div.stepper",
    h("button.icon", { onclick: () => step(-1), "aria-label": "-" }, icon("minus")), val,
    h("button.icon", { onclick: () => step(1), "aria-label": "+" }, icon("plus")));

  const ingList = h("div.r-ings", rec.ingredients.map((i) => {
    if (i.heading) return h("div.r-sub", i.heading);
    const subs = i.subs?.length ? h("div.r-subs.hidden", h("b", `${t("subs")}: `), i.subs.join(" · ")) : null;
    return h("div.r-ing",
      h("div.line", h("span.txt", i.text),
        subs && h("button.icon.ghost", { title: t("subs"), onclick: () => subs.classList.toggle("hidden") }, "⇄"),
        h("button.icon.ghost", { title: t("addOne"), onclick: (e) => act(async () => {
          await P("shopping", { body: { name: i.text } }); toast(t("added"));
        }, e.currentTarget) }, icon("cart"))),
      subs);
  }));

  const steps = [];
  let n = 0;
  for (const s of rec.steps) {
    if (s.startsWith("# ")) { steps.push(h("div.r-sub", s.slice(2))); continue; }
    steps.push(h("div.r-step", h("span.n", ++n), h("p", s)));
  }

  const notes = h("textarea", { rows: 3, placeholder: t("notesPh"), value: rec.notes });
  notes.addEventListener("change", () => act(async () => { await P(`recipe/${id}`, { method: "PUT", body: { notes: notes.value } }); rec.notes = notes.value; }));

  const tagInput = h("input", { placeholder: t("tagsF"), value: rec.tags.join(", ") });
  tagInput.addEventListener("change", () => upd({ tags: tagInput.value }));

  fill(view,
    h("div.r-hero", rec.image ? h("img", { src: rec.image, alt: "" }) : h("div.ph", icon("utensils")),
      h("button.icon.back", { onclick: () => go("#/recipes/mine"), "aria-label": t("back") }, icon("left"))),
    h("div.r-titlebar", h("h1", rec.title), heart),
    h("div.row.wrap.small.muted", { style: { gap: "8px" } },
      kcalChip, meta && h("span", meta), rec.edited && h("span.chip.warn", t("edited")),
      rec.source_url && h("a", { href: rec.source_url, target: "_blank", rel: "noopener" }, icon("link"), " ", rec.site || t("source"))),
    h("div.row.wrap", { style: { gap: "14px" } }, stars, h("div.spacer"),
      h("button", { onclick: () => logPortion(rec) }, icon("utensils"), t("logPortion"))),
    h("div.r-cols",
      h("div.card.stack", { style: { gap: "10px" } },
        h("div.row", h("h2", t("ingredients")), h("div.spacer"), h("span.small.muted", t("servings")), sv),
        ingList,
        h("button", { onclick: (e) => act(async () => {
          await P("shopping", { body: { recipe_id: rec.id, servings: rec.servings } }); toast(t("added"));
        }, e.currentTarget) }, icon("cart"), t("addAll"))),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("steps")), steps.length ? steps : h("div.muted", "–"))),
    h("div.card.stack", { style: { gap: "8px" } }, h("h2", t("notes")), notes,
      h("label.field", t("tagsF").split(" (")[0], tagInput)),
    h("div.row.wrap", { style: { gap: "8px" } },
      h("button", { onclick: () => editMode(view, rec, ctx) }, icon("edit"), t("edit")),
      hasPlugin("sync") && h("button", { onclick: (e) => act(async () => {
        const r = await api("p/sync/recipes/share", { body: { recipe_id: rec.id } });
        const el = h("div.toast", `${t("shared")} `, h("a", { href: `#/social/recipe/${encodeURIComponent(r.slug)}` }, t("view")));
        document.getElementById("toasts").append(el);
        setTimeout(() => el.remove(), 7000);
      }, e.currentTarget) }, icon("users"), t("share")),
      rec.has_original && rec.edited && h("button", { onclick: () => confirm(t("resetQ")) && act(async () => {
        await P(`recipe/${id}/reset`, { body: {} }); ctx.servings = 0; await ctx.reload(); ctx.paint();
      }) }, icon("refresh"), t("reset")),
      h("div.spacer"),
      h("button.danger", { onclick: () => confirm(t("delQ")) && act(async () => {
        await P(`recipe/${id}`, { method: "DELETE" }); go("#/recipes/mine");
      }) }, icon("trash"), t("del"))));
}

function editMode(view, rec, ctx) {
  const title = h("input", { value: rec.title });
  const sv = h("input", { type: "number", min: 1, step: 1, value: rec.base_servings });
  const orig = rec.kcal_est ? "" : String(rec.kcal || "");
  const kcal = h("input", { type: "number", min: 0, value: orig, placeholder: rec.kcal_est ? `~${rec.kcal}` : "" });
  const ing = h("textarea", { rows: Math.min(18, rec.ingredient_lines.length + 2), value: rec.ingredient_lines.join("\n") });
  const steps = h("textarea", { rows: Math.min(22, rec.steps.length * 3 + 2), value: rec.steps.join("\n") });
  fill(view, h("h1", t("edit")), h("div.card.stack",
    h("label.field", t("titleF"), title), h("label.field", t("servings"), sv),
    h("label.field", t("kcalF"), kcal), h("label.field", t("ingF"), ing), h("label.field", t("stepF"), steps),
    h("div.row", { style: { gap: "8px" } },
      h("button.primary", { onclick: (e) => act(async () => {
        const patch = { title: title.value, servings: +sv.value, ingredients: ing.value, steps: steps.value };
        if (kcal.value !== orig) patch.kcal = kcal.value === "" ? null : +kcal.value;
        ctx.servings = +sv.value;
        await ctx.reload(patch); ctx.paint();
      }, e.currentTarget) }, icon("check"), t("save")),
      h("button", { onclick: () => ctx.paint() }, t("cancel")))));
}

function logPortion(rec) {
  const n = h("input", { type: "number", min: 0.25, step: 0.25, value: 1 });
  const k = h("input", { type: "number", min: 0, placeholder: rec.kcal ? String(rec.kcal) : "kcal",
                         value: rec.kcal ? "" : "" });
  const total = h("div.small.muted");
  const upd = () => { const per = +k.value || rec.kcal || 0; total.textContent = per ? `= ${Math.round(per * (+n.value || 0))} kcal` : ""; };
  n.oninput = k.oninput = upd; upd();
  const s = sheet(t("logPortion"), [
    h("div", h("b", rec.title)),
    h("label.field", t("portions"), n),
    h("label.field", `${t("kcalServing")}${rec.kcal ? "" : ""}`, k), total,
    h("button.primary.lg", { onclick: (e) => act(async () => {
      await P("log", { body: { recipe_id: rec.id, servings: +n.value, ...(k.value ? { kcal: +k.value } : {}), day: localDay() } });
      s.close(); toast(t("logged"));
    }, e.currentTarget) }, t("logPortion"))]);
}

// ---------------------------------------------------------------- shopping

async function renderShopping(el) {
  const list = h("div");
  const input = h("input", { placeholder: t("shopPh"), enterkeyhint: "done" });
  const addForm = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!input.value.trim()) return;
    act(async () => { const r = await P("shopping", { body: { name: input.value } }); input.value = ""; paint(r.items); }); } },
    h("div.r-input", icon("plus"), input), h("button.primary", { type: "submit" }, t("add")));
  fill(el, addForm, list);
  const call = (path, opts) => act(async () => paint((await P(path, opts)).items));

  function paint(items) {
    const open = items.filter((i) => !i.checked), done = items.filter((i) => i.checked);
    const row = (i) => {
      const subs = i.subs?.length ? h("div.r-subs.hidden", h("b", `${t("subs")}: `), i.subs.join(" · ")) : null;
      return h(`div.item.r-shop${i.checked ? ".done" : ""}`,
        h("input", { type: "checkbox", checked: i.checked, onchange: (e) => call(`shopping/${i.id}`, { method: "PATCH", body: { checked: e.target.checked } }) }),
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("div.txt", i.text),
          i.recipes.length ? h("div.tiny.muted.ellipsis", `${t("fromRecipes")} ${i.recipes.join(", ")}`) : null, subs),
        subs && h("button.icon.ghost", { title: t("subs"), onclick: () => subs.classList.toggle("hidden") }, "⇄"),
        h("button.icon.ghost", { onclick: () => call(`shopping/${i.id}`, { method: "DELETE" }), "aria-label": t("del") }, icon("x")));
    };
    fill(list,
      items.length ? [
        h("div.list", [...open, ...done].map(row)),
        h("div.row", { style: { gap: "8px", marginTop: "12px" } },
          done.length ? h("button", { onclick: () => call("shopping", { method: "DELETE" }) }, icon("check"), t("clearChecked")) : null,
          h("button.danger", { onclick: () => confirm(t("clearAll") + "?") && call("shopping?all=1", { method: "DELETE" }) }, icon("trash"), t("clearAll")))]
        : h("div.empty", t("shopEmpty")));
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
    const mName = h("input", { placeholder: t("name") });
    const mKcal = h("input", { type: "number", min: 0, placeholder: t("kcal"), style: { maxWidth: "110px" } });
    const manual = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); act(async () => {
      await P("log", { body: { day, name: mName.value, kcal: +mKcal.value } }); paint(); }); } },
      mName, mKcal, h("button.primary", { type: "submit" }, icon("plus")));
    const pick = h("select", h("option", { value: "" }, t("pick")),
      recipes.map((r) => h("option", { value: r.id }, r.title + (r.kcal ? ` (${kcalText(r)})` : ""))));
    const por = h("input", { type: "number", min: 0.25, step: 0.25, value: 1, style: { maxWidth: "90px" } });
    const fromBook = h("form.r-search", { onsubmit: (e) => { e.preventDefault(); if (!pick.value) return; act(async () => {
      await P("log", { body: { day, recipe_id: +pick.value, servings: +por.value } }); paint(); }); } },
      pick, por, h("button.primary", { type: "submit" }, icon("plus")));

    const max = Math.max(nut.goal * 1.15, ...nut.days.map((d) => d.kcal));
    const bars = h("div.r-bars", nut.days.map((d) => h("div.bar", { title: `${d.day}: ${Math.round(d.kcal)} kcal`,
      onclick: () => { day = d.day; paint(); } },
      h("div.col", h("div.goal", { style: { bottom: `${nut.goal / max * 100}%` } }), h(`div.fill${d.kcal > nut.goal ? ".over" : ""}${d.day === day ? ".sel" : ""}`, { style: { height: `${d.kcal / max * 100}%` } })),
      h("div.lbl", new Date(d.day + "T12:00").toLocaleDateString(LANG ? "en" : "it", { weekday: "short" })),
      h("div.val", d.kcal ? Math.round(d.kcal) : ""))));

    fill(body,
      h("div.card.stack", { style: { gap: "10px" } },
        h("div.row", { style: { gap: "10px" } }, dayInput, h("div.spacer"), h("span.muted.small", `${t("goal")} ${Math.round(log.goal)} kcal`)),
        h("div.r-total", h("span.big", Math.round(log.total)), h("span.muted", ` kcal ${t("eaten")}`)),
        h("div.progress", h(`div${log.total > log.goal ? ".over" : ""}`, { style: { width: `${pct}%` } })),
        h("div.small.muted", diff >= 0 ? `${diff} kcal ${t("left")}` : `${-diff} kcal ${t("over")}`)),
      log.entries.length
        ? h("div.list", log.entries.map((e) => h("div.item",
          h("div.stack", { style: { gap: "2px", flex: 1, minWidth: 0 } }, h("div.ellipsis", e.name),
            h("div.tiny.muted", [e.time, e.servings && `× ${e.servings}`, e.est && "~"].filter(Boolean).join(" · "))),
          h("b", `${e.est ? "~" : ""}${Math.round(e.kcal)}`), h("span.muted.small", "kcal"),
          h("button.icon.ghost", { onclick: () => act(async () => { await P(`log/${e.id}`, { method: "DELETE" }); paint(); }), "aria-label": t("del") }, icon("x")))))
        : h("div.empty", t("logEmpty")),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("fromBook")), fromBook, h("h2", t("manual")), manual),
      h("div.card.stack", { style: { gap: "10px" } }, h("h2", t("last7")), bars));
  }
  await paint();
}
