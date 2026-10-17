// Bluetooth sensors panel (inside the plugin's settings sheet): nearby devices + add/remove/ignore.
import { api, h, icon, toast, act, t } from "../app.js";

const KEYS = [["temperature", 1], ["humidity", 0], ["co2", 0], ["pressure", 0], ["illuminance", 0],
              ["moisture", 0], ["battery", 0], ["voltage", 2]];

function values(d) {
  const parts = [];
  for (const [k, dec] of KEYS) {
    const v = d.values?.[k];
    if (typeof v !== "number") continue;
    const u = d.units?.[k] || "";
    parts.push(`${v.toFixed(dec)}${u === "%" || u === "°C" ? "" : " "}${u}`.replace(/(\d) ?°C/, "$1 °C"));
  }
  return parts.join(" · ");
}

function bars(rssi) {
  const n = rssi == null ? 0 : rssi > -60 ? 4 : rssi > -70 ? 3 : rssi > -82 ? 2 : 1;
  return h("span.rssi", { title: rssi == null ? t("not heard") : `${rssi} dBm`, style: { display: "inline-flex", alignItems: "flex-end", gap: "2px", height: "14px" } },
    [1, 2, 3, 4].map((i) => h("i", { style: { display: "block", width: "3px", height: `${3 + i * 2.5}px`, borderRadius: "1px",
      background: "currentColor", opacity: i <= n ? 0.9 : 0.2 } })));
}

export default async function (container, plugin) {
  const hint = h("p.small.muted");
  const list = h("div.list");
  container.append(hint, list);
  let busy = false;

  const call = (path, body) => api(`/api/p/bluetooth/${path}`, { method: "POST", body });

  function row(d) {
    const title = d.name || d.address;
    const buttons = [];
    if (d.added) buttons.push(h("button.btn.small", { onclick: (ev) => act(async () => { await call("remove", { address: d.address }); toast(t("Removed")); await load(); }, ev.currentTarget) }, t("Remove")));
    else if (d.recognised) buttons.push(h("button.btn.small.primary", { onclick: (ev) => act(async () => { await call("add", { address: d.address }); toast(t("Added")); await load(); }, ev.currentTarget) }, t("Add")));
    if (!d.added) buttons.push(h("button.btn.small", { onclick: (ev) => act(async () => { await call("ignore", { address: d.address, ignore: !d.ignored }); await load(); }, ev.currentTarget) }, d.ignored ? t("Unignore") : t("Ignore")));
    return h("div.row", { style: { opacity: d.ignored ? 0.5 : 1, gap: "10px", alignItems: "center" } },
      icon(d.recognised ? "thermo" : "bluetooth"),
      h("div.grow", { style: { minWidth: 0, flex: "1" } },
        h("div", title, d.added && h("span.small.muted", ` · ${t("added")}`)),
        h("div.small.muted", [d.model !== "unknown" ? d.model : t("unknown device"), d.name ? d.address : null].filter(Boolean).join(" · ")),
        values(d) && h("div.small", values(d))),
      bars(d.rssi),
      h("div.row", { style: { gap: "6px" } }, buttons));
  }

  async function load() {
    if (busy) return;
    busy = true;
    try {
      const r = await api("/api/p/bluetooth/nearby");
      const err = r.error || plugin?.error;
      hint.textContent = err ? err
        : r.scanning ? (r.devices.length ? "" : t("Scanning... no devices heard yet."))
        : t("Bluetooth scanning is not running.");
      hint.style.color = err ? "var(--bad, #c0392b)" : "";
      list.replaceChildren(...r.devices.map(row));
    } catch (e) {
      hint.textContent = e.message;
    } finally { busy = false; }
  }

  await load();
  const timer = setInterval(() => {
    if (!container.isConnected) return clearInterval(timer);
    load();
  }, 3000);
}
