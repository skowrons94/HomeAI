// More ("Altro"): who is using this phone, every page that isn't in the tab bar, notifications,
// and the tab bar layout (saved per person on the hub, see savePrefs in app.js). Grouped like
// the iOS Settings app: a profile card, then titled lists.
import { h, icon, t, store, go, openNotifications, person, choosePerson, savePrefs,
         pinnable, pinnedTabs, MAX_TABS, act, toast, pushState, enablePush, disablePush } from "../app.js";

// one line under each page so nobody has to guess what "Rete" is
const PAGE_HINT = () => ({
  history: t("Charts of temperatures and usage"),
  network: t("Devices connected at home"),
  settings: t("Integrations, password, language"),
  social: t("What friends watched and cooked"),
  home: t("The house at a glance"), media: t("Movies and series"), recipes: t("Recipes and shopping list"),
});

const row = (ico, label, sub, onclick, extra) =>
  h("div.item.m-row", { onclick, role: onclick ? "button" : null, tabindex: onclick ? 0 : null },
    h("span.m-ico", icon(ico)),
    h("div.stack.spacer", { style: { gap: "1px", minWidth: 0 } }, h("span", label), sub && h("span.muted.small", sub)),
    extra, onclick && h("span.muted", icon("right")));

export default {
  async render(root) {
    const pinned = pinnedTabs();
    const others = pinnable().filter((p) => !pinned.includes(p.id));
    root.append(...[
      h("header.page", h("h1", t("More"))),

      store.people.length ? h("div.card.m-profile", { onclick: () => choosePerson(), role: "button", tabindex: 0 },
        h("span.m-avatar", (person() || "?").slice(0, 1).toUpperCase()),
        h("div.stack.spacer", { style: { gap: "1px" } },
          h("b", person() || t("Nobody chosen")), h("span.muted.small", t("Who is using this phone"))),
        h("span.chip.accent", t("Change"))) : null,

      others.length ? [
        h("div.grp-head", t("Pages")),
        h("div.list", others.map((p) => row(p.icon, t(p.title), PAGE_HINT()[p.id], () => go(`#/${p.id}`),
          p.id === "settings" && store.attention ? h("span.chip.warn", store.attention) : null))),
      ] : null,

      h("div.grp-head", t("Notifications")),
      h("div.list",
        row("bell", t("Received notifications"), null, openNotifications,
            store.unread ? h("span.chip.accent", store.unread) : null),
        pushRow()),
      h("p.grp-foot", t("Casa tells you when a film is ready to watch, when subtitles are found or when something at home needs you.")),

      h("div.grp-head", t("Tab bar")),
      tabEditor(),
      h("p.grp-foot", t("Up to {n} pages next to More. Everything else stays here.", { n: MAX_TABS })),
    ].flat().filter(Boolean));
  },
};

const PUSH_HINT = {
  "needs-https": "Open Casa from its Tailscale address (https://…) to turn them on.",
  "needs-home-screen": "In Safari tap Share → Add to Home Screen, open Casa from there and turn them on here.",
  unsupported: "This browser can't receive notifications.",
  denied: "Notifications are blocked: allow them for Casa in the phone's Settings.",
};

function pushRow() {
  const box = h("div.item.m-row", h("span.m-ico", icon("remote")), h("span.spacer", "…"));
  const draw = async () => {
    const st = await pushState().catch(() => "unsupported");
    const on = st === "on";
    const btn = h(`button${on ? "" : ".primary"}`, { disabled: !["on", "off"].includes(st), onclick: () => act(async () => {
      if (on) { await disablePush(); toast(t("Notifications turned off")); }
      else { await enablePush(); toast(t("Notifications turned on")); }
      draw();
    }, btn) }, on ? t("Disable") : t("Enable"));
    box.replaceChildren(h("span.m-ico", icon("remote")),
      h("div.stack.spacer", { style: { gap: "2px", minWidth: 0 } },
        h("span", t("On this phone")),
        h("span.muted.small", on ? t("On: Casa can notify this phone") : PUSH_HINT[st] ? t(PUSH_HINT[st]) : t("Notifications off"))),
      btn);
  };
  draw();
  return box;
}

/** Two groups like iOS edit lists: in the bar (move, remove) and other pages (add). */
function tabEditor() {
  const box = h("div.stack", { style: { gap: "10px" } });
  let tabs = [...pinnedTabs()];
  const save = () => savePrefs({ tabs });
  const all = pinnable();
  const page = (id) => all.find((x) => x.id === id);
  const inBar = tabs.map((id, i) => h("div.item.m-row",
    h("button.icon.m-rm", { "aria-label": t("Remove from the tab bar"), onclick: () => { tabs = tabs.filter((x) => x !== id); save(); } }, icon("minus")),
    h("span.m-ico", icon(page(id).icon)), h("span.spacer", t(page(id).title)),
    h("button.icon.ghost", { "aria-label": t("Move up"), disabled: i === 0,
      onclick: () => { [tabs[i - 1], tabs[i]] = [tabs[i], tabs[i - 1]]; save(); } }, icon("up")),
    h("button.icon.ghost", { "aria-label": t("Move down"), disabled: i === tabs.length - 1,
      onclick: () => { [tabs[i + 1], tabs[i]] = [tabs[i], tabs[i + 1]]; save(); } }, icon("down"))));
  const rest = all.filter((p) => !tabs.includes(p.id)).map((p) => h("div.item.m-row",
    h("button.icon.m-add", { "aria-label": t("Add to the tab bar"), disabled: tabs.length >= MAX_TABS,
      onclick: () => { tabs = [...tabs, p.id]; save(); } }, icon("plus")),
    h("span.m-ico", icon(p.icon)), h("span.spacer", t(p.title))));
  box.append(
    h("div.list", h("div.item.m-sub", t("In the bar")), inBar.length ? inBar : h("div.item.muted", t("Nothing yet."))),
    rest.length ? h("div.list", h("div.item.m-sub", t("Other pages")), rest) : "");
  return box;
}
