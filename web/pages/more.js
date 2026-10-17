// More ("Altro"): every page that isn't in the phone tab bar, notifications, who is using this
// phone, and the tab bar layout (saved per person on the hub, see savePrefs in app.js).
import { h, icon, t, store, go, openNotifications, person, choosePerson, savePrefs,
         pinnable, pinnedTabs, MAX_TABS, LANG, act, toast, pushState, enablePush, disablePush } from "../app.js";

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

      h("div.section", h("h2", t("Notifications on this phone"))),
      pushRow(),

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

const PUSH_HINT = {
  "needs-https": "Open Casa from its Tailscale address (https://…) to turn them on.",
  "needs-home-screen": "In Safari tap Share → Add to Home Screen, open Casa from there and turn them on here.",
  unsupported: "This browser can't receive notifications.",
  denied: "Notifications are blocked: allow them for Casa in the phone's Settings.",
};

function pushRow() {
  const box = h("div.list");
  const draw = async () => {
    const st = await pushState().catch(() => "unsupported");
    const on = st === "on";
    const btn = h(`button${on ? "" : ".primary"}`, { disabled: !["on", "off"].includes(st), onclick: () => act(async () => {
      if (on) { await disablePush(); toast(t("Notifications turned off")); }
      else { await enablePush(); toast(t("Notifications turned on")); }
      draw();
    }, btn) }, on ? t("Disable") : t("Enable"));
    box.replaceChildren(h("div.item", icon("bell"),
      h("div.stack.spacer", { style: { gap: "2px" } },
        h("span", on ? t("On: Casa can notify this phone") : t("Notifications off")),
        PUSH_HINT[st] && h("span.muted.small", t(PUSH_HINT[st]))),
      btn));
  };
  draw();
  return box;
}
