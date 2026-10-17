// Network: everything the hub sees on the LAN (mDNS + ARP), from the network plugin.
import { api, h, icon, bus, fmt, debounce } from "../app.js";

const KIND_ICON = { apple_tv: "appletv", homepod: "speaker", speaker: "speaker", tv: "tv", nas: "nas",
                    computer: "appletv", hub: "homekit", homekit: "homekit", phone: "remote",
                    printer: "sensor", router: "wifi", boiler: "flame", lights: "bulb" };

export default {
  async render(root) {
    let devices = [], filter = "";
    const list = h("div");
    const q = h("input", { placeholder: "Filter by name, IP, model…", type: "search",
                           oninput: debounce((e) => { filter = e.target.value.toLowerCase(); draw(); }, 150) });
    root.append(
      h("header.page", h("h1", "Network"), h("div.spacer"),
        h("button.icon", { title: "Refresh", onclick: load }, icon("refresh"))),
      h("div", { style: { marginBottom: "14px" } }, q),
      list);

    async function load() {
      try { devices = await api("/api/p/network/devices"); draw(); }
      catch (e) { list.replaceChildren(h("div.empty", "The network plugin isn't enabled (", h("code", "[plugins.network]"), " in config.toml).")); }
    }

    function draw() {
      const shown = devices.filter((d) => !filter ||
        [d.name, d.ip, d.model, d.manufacturer, d.vendor, d.kind, d.mac].join(" ").toLowerCase().includes(filter));
      const online = shown.filter((d) => d.online), offline = shown.filter((d) => !d.online);
      const section = (title, arr) => arr.length ? [
        h("div.section", h("h2", title), h("span.muted.small", arr.length)),
        h("div.list", arr.sort((a, b) => !a.name - !b.name || (a.name || a.ip).localeCompare(b.name || b.ip, undefined, { numeric: true })).map(row)),
      ] : [];
      list.replaceChildren(...section("Online", online), ...section("Recently seen", offline),
        ...(shown.length ? [] : [h("div.empty", "Nothing found yet — discovery takes a few seconds.")]));
    }

    function row(d) {
      return h("div.item",
        h("div.tile", { style: { minHeight: 0 } }, h(`div.ico`, icon(KIND_ICON[d.kind] || "wifi"))),
        h("div.stack", { style: { gap: "3px", minWidth: 0, flex: 1 } },
          h("div.row", h("b.ellipsis", d.name || d.ip), d.online ? h("span.chip.ok", "online") : h("span.chip", fmt.ago(d.last_seen))),
          h("div.small.muted.ellipsis", [d.ip, d.manufacturer || d.vendor, d.model, d.mac].filter(Boolean).join(" · ")),
          d.services?.length ? h("div.row.wrap", { style: { gap: "4px" } },
            d.services.slice(0, 8).map((s) => h("span.chip.tiny", s.replace(/^_|\._(tcp|udp).*$/g, "")))) : null),
        d.configured ? h("span.chip.accent", "in Casa")
          : d.suggested_plugin ? h("span.chip.warn", { title: `Enable [plugins.${d.suggested_plugin}]` }, `+ ${d.suggested_plugin}`) : null);
    }

    const onNet = debounce(load, 2000);
    bus.addEventListener("network", onNet);
    await load();
    return () => bus.removeEventListener("network", onNet);
  },
};
