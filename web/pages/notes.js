// Household notes: sticky-note cards, tick checklists in place, edit in a sheet (hub/plugins/notes.py).
// Route: #/notes. Live: the hub publishes event "notes" ({items}) after every change.
import { api, h, icon, toast, sheet, ensureCss, act, bus, fmt, t } from "../app.js";

const P = (path, opts) => api(`p/notes/${path}`, opts);
const COLORS = ["yellow", "blue", "green", "pink", "gray"];
const COLOR_LABEL = () => ({ yellow: t("Yellow"), blue: t("Blue"), green: t("Green"), pink: t("Pink"), gray: t("Gray") });

let notes = [];
let grid = null;
let editing = false;       // a sheet is open: don't re-render under the user's fingers
let stale = false;         // data changed while the sheet was open

const draw = () => {
  if (!grid || !grid.isConnected) return;
  if (editing) { stale = true; return; }
  const cards = notes.map(card);
  grid.replaceChildren(...(cards.length ? cards : [empty()]));
  requestAnimationFrame(() => grid.querySelectorAll(".n-text").forEach((el) => el.classList.toggle("cut", el.scrollHeight > el.clientHeight + 1)));
};

const save = async (id, patch) => {
  const n = await P(`notes/${id}`, { method: "PATCH", body: patch });
  notes = notes.map((x) => (x.id === id ? n : x));
  return n;
};

function empty() {
  return h("div.empty.n-empty", h("div.n-empty-ic", icon("edit")),
    h("p", t("No notes yet. Leave a reminder for the house.")),
    h("button.primary", { onclick: () => openNote() }, icon("plus"), t("New note")));
}

function card(n) {
  const items = (n.items || []);
  const list = items.length ? h("div.n-items", items.map((it, i) =>
    h("label.n-item" + (it.done ? ".done" : ""), { onclick: (e) => e.stopPropagation() },
      h("input", { type: "checkbox", checked: it.done, onchange: (e) => {
        const next = items.map((x, j) => (j === i ? { ...x, done: e.target.checked } : x));
        act(async () => { await save(n.id, { items: next }); draw(); });
      } }),
      h("span", it.text)))) : null;
  return h(`div.n-card.c-${n.color}`, { onclick: () => openNote(n) },
    n.pinned ? h("span.n-pin", { title: t("Pinned") }, "📌") : null,
    n.text ? h("div.n-text", n.text) : null,
    list,
    h("div.n-foot", [n.author, fmt.ago(n.updated)].filter(Boolean).join(" · ")));
}

function openNote(n) {
  const isNew = !n;
  const cur = n ? { text: n.text, items: (n.items || []).map((i) => ({ ...i })), color: n.color, pinned: n.pinned }
                : { text: "", items: [], color: "yellow", pinned: false };
  editing = true;
  const text = h("textarea.n-area", { rows: 4, placeholder: t("Write a note…"), value: cur.text,
                                     oninput: (e) => { cur.text = e.target.value; } });
  const itemsEl = h("div.n-edit-items");
  const focusItem = (i) => setTimeout(() => itemsEl.querySelectorAll("input[type=text]")[i]?.focus(), 0);
  const drawItems = () => itemsEl.replaceChildren(...cur.items.map((it, i) =>
    h("div.n-edit-item",
      h("input", { type: "checkbox", checked: it.done, onchange: (e) => { it.done = e.target.checked; } }),
      h("input", { type: "text", value: it.text, placeholder: t("Item"), maxlength: 200, enterkeyhint: "next",
                   oninput: (e) => { it.text = e.target.value; },
                   onkeydown: (e) => {
                     if (e.key !== "Enter") return;
                     e.preventDefault();
                     cur.items.splice(i + 1, 0, { text: "", done: false });
                     drawItems(); focusItem(i + 1);
                   } }),
      h("button.icon.ghost", { type: "button", "aria-label": t("Remove item"),
                               onclick: () => { cur.items.splice(i, 1); drawItems(); } }, icon("x")))));
  drawItems();
  const addItem = () => { cur.items.push({ text: "", done: false }); drawItems(); focusItem(cur.items.length - 1); };

  const dots = h("div.n-colors");
  const drawDots = () => dots.replaceChildren(...COLORS.map((c) =>
    h(`button.n-dot.c-${c}${cur.color === c ? ".sel" : ""}`, { type: "button", title: COLOR_LABEL()[c],
      "aria-label": COLOR_LABEL()[c], "aria-pressed": cur.color === c, onclick: () => { cur.color = c; drawDots(); } })));
  drawDots();

  const pin = h("label.n-pinrow", h("input", { type: "checkbox", checked: cur.pinned, onchange: (e) => { cur.pinned = e.target.checked; } }),
    h("span", t("Pin to the top")));

  let sh;
  const finish = () => { sh.close(); };
  const body = () => ({
    text: cur.text.trim(),
    items: cur.items.filter((i) => i.text.trim()).map((i) => ({ text: i.text.trim(), done: i.done })),
    color: cur.color, pinned: cur.pinned,
  });
  const saveBtn = h("button.primary", { type: "button", onclick: () => act(async () => {
    const b = body();
    if (isNew) notes = [await P("notes", { method: "POST", body: b }), ...notes];
    else await save(n.id, b);
    toast(t("Note saved"));
    editing = false; finish();
  }, saveBtn) }, icon("check"), t("Save"));
  const del = isNew ? null : h("button.danger", { type: "button", onclick: () => {
    if (!confirm(t("Delete this note?"))) return;
    act(async () => {
      await P(`notes/${n.id}`, { method: "DELETE" });
      notes = notes.filter((x) => x.id !== n.id);
      toast(t("Note deleted"));
      editing = false; finish();
    });
  } }, icon("trash"), t("Delete"));

  sh = sheet(isNew ? t("New note") : t("Edit note"), [
    text,
    h("div.n-label", t("Checklist")),
    itemsEl,
    h("button.n-add", { type: "button", onclick: addItem }, icon("plus"), t("Add item")),
    h("div.n-label", t("Colour")), dots,
    pin,
    h("div.row.n-actions", del, h("div.spacer"), saveBtn),
  ], { onClose: () => { editing = false; if (stale) { stale = false; refresh(); } else draw(); } });
  if (isNew) setTimeout(() => text.focus(), 50);
}

async function refresh() {
  try { notes = await P("notes"); } catch (e) { toast(e.message, true); }
  draw();
}

let listening = false;
function listen() {
  if (listening) return;
  listening = true;
  bus.addEventListener("notes", (e) => {
    const d = e.detail?.items ?? e.detail?.data?.items;
    if (Array.isArray(d)) notes = d;
    draw();
  });
}

export default {
  async render(root, args) {
    ensureCss(`/static/pages/notes.css?v=${window.CASA_V}`);
    listen();
    editing = false; stale = false;
    grid = h("div.n-grid");
    root.replaceChildren(
      h("div.n-head", h("h1", t("Notes")), h("button.primary", { onclick: () => openNote() }, icon("plus"), t("New note"))),
      grid);
    await refresh();
  },
};
