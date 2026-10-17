// More ("Altro"): every page that isn't in the phone tab bar, notifications, who is using this
// phone, and the tab bar layout (saved per person on the hub, see savePrefs in app.js).
import { h, icon, t, store, go, openNotifications, person, choosePerson, savePrefs,
         pinnable, pinnedTabs, MAX_TABS, LANG } from "../app.js";

export default {
  async render(root) {
    const pinned = pinnedTabs();
    const others = pinnable().filter((p) => !pinned.includes(p.id));
    const row = (ico, label, onclick, extra) =>
      h("div.item", { onclick, style: { cursor: "pointer" } }, icon(ico), h("span.spacer", label), extra, icon("right"));

    root.append(...[
      h("header.page", h("h1", t("More"))),
      h("div.list",
        row("bell", t("Notifications"), openNotifications,
            store.unread ? h("span.chip.accent", store.unread) : null),
        others.map((p) => row(p.icon, t(p.title), () => go(`#/${p.id}`),
          p.id === "settings" && store.attention ? h("span.chip.warn", store.attention) : null))),

      store.people.length ? [
        h("div.section", h("h2", t("Who is using this phone"))),
        h("div.list", row("users", person() || t("Nobody chosen"), () => choosePerson(),
                          h("span.muted.small", t("Change")))),
      ] : null,

      h("div.section", h("h2", t("Tab bar"))),
      h("p.muted.small", { style: { margin: "0 0 10px" } },
        t("Up to {n} pages next to More. Everything else stays here.", { n: MAX_TABS })),
      tabEditor(),

      h("div.section", h("h2", t("Language"))),
      h("div.list", row("flag", LANG === "it" ? "Italiano" : "English", () => go("#/settings"),
                        h("span.muted.small", t("Settings")))),
    ].flat().filter(Boolean));
  },
};

function tabEditor() {
  const box = h("div.list");
  let tabs = [...pinnedTabs()];
  const save = () => savePrefs({ tabs });
  const draw = () => {
    const all = pinnable();
    const order = [...tabs, ...all.map((p) => p.id).filter((id) => !tabs.includes(id))];
    box.replaceChildren(...order.map((id) => {
      const p = all.find((x) => x.id === id);
      const on = tabs.includes(id), i = tabs.indexOf(id);
      return h("div.item",
        icon(p.icon), h("span.spacer", t(p.title)),
        on && h("button.icon.ghost", { title: t("Move up"), disabled: i === 0,
          onclick: () => { [tabs[i - 1], tabs[i]] = [tabs[i], tabs[i - 1]]; save(); } }, icon("up")),
        on && h("button.icon.ghost", { title: t("Move down"), disabled: i === tabs.length - 1,
          onclick: () => { [tabs[i + 1], tabs[i]] = [tabs[i], tabs[i + 1]]; save(); } }, icon("down")),
        h(`button.icon${on ? ".primary" : ""}`, {
          title: on ? t("Remove from the tab bar") : t("Add to the tab bar"),
          disabled: !on && tabs.length >= MAX_TABS,
          onclick: () => { tabs = on ? tabs.filter((x) => x !== id) : [...tabs, id]; save(); },
        }, icon(on ? "check" : "plus")));
    }));
  };
  draw();
  return box;
}
