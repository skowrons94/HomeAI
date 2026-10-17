// "Needs attention": guided fix-it flow (list + step-by-step), shared by Home, Settings and the nav badge.
import { api, h, icon, sheet, store, toast, act, bus, go, refreshState, refreshAttention, ensureCss, t } from "../app.js";

const LEVELS = [
  { id: "error", get title() { return t("Not working"); }, icon: "x" },
  { id: "warning", get title() { return t("Needs setup"); }, icon: "bell" },
  { id: "info", get title() { return t("Suggestions"); }, icon: "bulb" },
];
const actionLabel = (type) => ({ settings: t("Open settings"), setup: t("Pair"), homekit: t("Show code"), core: t("Configure"),
                                 page: t("Open"), url: t("Open link") }[type] || t("Fix"));
const css = () => ensureCss(`/static/pages/attention.css?v=${window.CASA_V}`);

export const fetchItems = () => api("attention");

/** Resolves once every sheet opened by `fn` has been closed. */
async function untilClosed(fn) {
  const before = new Set(document.querySelectorAll(".backdrop"));
  await fn();
  const mine = () => [...document.querySelectorAll(".backdrop")].filter((b) => !before.has(b));
  if (!mine().length) return;
  await new Promise((res) => {
    const t = setInterval(() => { if (!mine().length) { clearInterval(t); res(); } }, 250);
  });
}

async function coreSheet(section) {
  const data = await api("settings");
  const c = data.core.find((x) => x.name === section);
  if (!c) return;
  const { renderForm } = await import(`./settings.js?v=${window.CASA_V}`);
  const form = renderForm(c.schema || [], c.values || {});
  const s = sheet(c.title, [form.el, h("button.primary.lg", { onclick: (e) => act(async () => {
    const r = await api(`settings/core/${section}`, { method: "PUT", body: { values: form.values() } });
    toast(r.restart ? t("Saved - restart to apply") : t("Saved"));
    s.close(); refreshState().catch(() => {});
  }, e.currentTarget) }, t("Save"))]);
}

function homekitSheet() {
  const box = h("div.stack", h("div.row", h("span.spin"), h("span.muted", t("Loading..."))));
  sheet("Apple Home", box);
  api("homekit").then((hk) => {
    if (!hk || !hk.running) return box.replaceChildren(h("div.muted", t("The HomeKit bridge is not running.")));
    box.replaceChildren(h("div.stack", { style: { alignItems: "center", textAlign: "center" } },
      h("img", { src: "/api/homekit/qr.svg", width: 200, height: 200, alt: t("HomeKit QR"),
                 style: { background: "#fff", borderRadius: "12px" } }),
      h("div", t("Setup code"), " ", h("b", { style: { fontSize: "22px", letterSpacing: ".05em" } }, hk.pincode)),
      h("div.small.muted", t("Home app, + , Add Accessory, scan the code and accept \"Uncertified Accessory\"."))));
  }).catch((e) => box.replaceChildren(h("div.muted", e.message)));
}

/** Run an item's action; resolves when whatever it opened has been closed. */
export async function runAction(item) {
  const a = item.action || {};
  const set = await import(`./settings.js?v=${window.CASA_V}`);
  switch (a.type) {
    case "settings": return untilClosed(() => set.pluginSheet(a.plugin));
    case "setup": {
      const d = await api("settings");
      const p = d.plugins.find((x) => x.name === a.plugin);
      if (!p) return toast(t("Plugin not found"), true);
      return untilClosed(() => set.setupWizard(p));
    }
    case "homekit": return untilClosed(() => homekitSheet());
    case "core": return untilClosed(() => coreSheet(a.section));
    case "page": return go(`#/${a.page}`);
    case "url": return void window.open(a.url, "_blank", "noopener");
  }
}

const dismiss = (item, on = true) => api(`attention/${encodeURIComponent(item.id)}`, { body: { dismissed: on } });

function row(item, { onRun, onDismiss }) {
  const lv = LEVELS.find((l) => l.id === item.level) || LEVELS[2];
  return h("div.item.at-item", { class: `lv-${lv.id}` },
    h("div.at-ico", icon(lv.icon)),
    h("div.at-txt", h("b", item.title), item.detail && h("span", item.detail)),
    h("div.at-btns",
      h("button.primary", { onclick: (e) => onRun(item, e.currentTarget) }, actionLabel(item.action?.type)),
      item.level !== "error" && h("button.ghost.small", { onclick: (e) => onDismiss(item, e.currentTarget) }, t("Dismiss"))));
}

/** Open the flow. mode: "list" (default) or "steps". */
export function openAttention(mode = "list") {
  css();
  const body = h("div.stack.at-flow");
  const s = sheet(t("Needs attention"), body, { onClose: () => refreshAttention().catch(() => {}) });
  let items = [], skipped = new Set(), total = 0;

  const reload = async () => {
    try { items = await fetchItems(); } catch (e) { toast(e.message, true); }
    refreshAttention().catch(() => {});
    draw();
  };
  const run = (item, btn) => act(async () => { await runAction(item); await reload(); }, btn);
  const doDismiss = (item, btn) => act(async () => { await dismiss(item); await reload(); }, btn);

  const empty = (text) => h("div.at-done", h("div.at-bigico", icon("check")),
    h("b", t("All set")), h("span.muted", text), h("button.primary.lg", { onclick: () => s.close() }, t("Close")));

  const drawList = () => {
    if (!items.length) return body.replaceChildren(empty(t("Nothing needs your attention right now.")));
    body.replaceChildren(
      h("button.primary.lg", { onclick: () => { skipped = new Set(); total = items.length; mode = "steps"; draw(); } },
        icon("right"), t("Fix one by one")),
      ...LEVELS.map((lv) => {
        const its = items.filter((i) => i.level === lv.id);
        return !its.length ? null : h("div.stack.at-group", { class: `lv-${lv.id}` },
          h("div.at-gh", h("span", lv.title), h("span.chip", its.length)),
          h("div.list", its.map((i) => row(i, { onRun: run, onDismiss: doDismiss }))));
      }).filter(Boolean));
  };

  const drawSteps = () => {
    const rest = items.filter((i) => !skipped.has(i.id));
    if (!rest.length) {
      return body.replaceChildren(skipped.size && items.length
        ? h("div.at-done", h("div.at-bigico.mute", icon("flag")), h("b", t("{n} left for later", { n: items.length })),
            h("span.muted", t("You skipped these. You can come back any time from Home or Settings.")),
            h("button.primary.lg", { onclick: () => { mode = "list"; draw(); } }, t("See the list")),
            h("button.ghost", { onclick: () => s.close() }, t("Close")))
        : empty(t("Everything is in order.")));
    }
    const it = rest[0], lv = LEVELS.find((l) => l.id === it.level) || LEVELS[2];
    total = Math.max(total, rest.length + skipped.size);
    body.replaceChildren(
      h("div.row.small.muted", h("span", t("Step {n} of {total}", { n: total - rest.length + 1, total })), h("div.spacer"),
        h("button.ghost.small", { onclick: () => { mode = "list"; draw(); } }, t("Back to list"))),
      h("div.at-prog", h("i", { style: { width: `${Math.round(100 * (total - rest.length) / total)}%` } })),
      h("div.card.at-step", { class: `lv-${lv.id}` },
        h("div.at-bigico", icon(lv.icon)),
        h("span.chip", lv.title),
        h("b.at-st", it.title),
        it.detail && h("div.muted", it.detail),
        h("button.primary.lg", { onclick: (e) => run(it, e.currentTarget) }, actionLabel(it.action?.type))),
      h("div.row",
        h("button", { onclick: () => { skipped.add(it.id); draw(); } }, rest.length > 1 ? t("Skip") : t("Skip for now")),
        it.level !== "error" && h("button.ghost", { onclick: (e) => doDismiss(it, e.currentTarget) }, t("Dismiss")),
        h("div.spacer"),
        rest.length > 1 && h("span.tiny.muted", t("{n} more", { n: rest.length - 1 }))));
  };

  const draw = () => (mode === "steps" ? drawSteps() : drawList());
  body.replaceChildren(h("div.row", h("span.spin"), h("span.muted", t("Loading..."))));
  fetchItems().then((x) => { items = x; total = x.length; draw(); }).catch((e) => toast(e.message, true));
  return s;
}

/** Compact card; updates itself. Hidden when there is nothing to show. */
export function attentionBanner() {
  css();
  const box = h("div.at-banner-box");
  const draw = () => {
    const items = store.attentionItems || [];
    const urgent = items.filter((i) => i.level !== "info");
    if (!items.length) return box.replaceChildren();
    if (!urgent.length) {
      return box.replaceChildren(h("button.at-info.ghost", { onclick: () => openAttention() },
        icon("bulb"), h("span", items.length === 1 ? items[0].title : t("{n} suggestions", { n: items.length })),
        h("span.muted", t("Review"))));
    }
    const lv = urgent.some((i) => i.level === "error") ? "error" : "warning";
    box.replaceChildren(h("div.card.at-banner", { class: `lv-${lv}` },
      h("div.at-ico", icon(lv === "error" ? "x" : "bell")),
      h("div.at-txt", h("b", urgent.length === 1 ? t("1 thing needs your attention") : t("{n} things need your attention", { n: urgent.length })),
        h("span", urgent[0].title)),
      h("button.primary", { onclick: () => openAttention() }, t("Review"))));
  };
  draw();
  const on = () => draw();
  bus.addEventListener("attentionchange", on);
  const mo = new MutationObserver(() => { if (!box.isConnected) { bus.removeEventListener("attentionchange", on); mo.disconnect(); } });
  mo.observe(document.body, { childList: true, subtree: true });
  return box;
}
