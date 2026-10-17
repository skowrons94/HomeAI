// Storage & NAS panel, shown inside the plugin's settings sheet.
import { api, h, icon, toast, act, sheet } from "../app.js";

const P = "/api/p/storage";
const LABELS = { download_dir: "Downloads", movies_dir: "Movies", series_dir: "Series", nas: "NAS" };

export default async function (container, plugin) {
  let shares = [];
  const form = { host: "", share: "", username: "" };
  const root = h("div.stack");
  container.append(root);

  const field = (key) => {
    const k = `nas.${key}`;
    return document.querySelector(`[name="${k}"], [data-key="${k}"], [id="${k}"]`);
  };
  const fill = (key, v) => { const el = field(key); if (el) { el.value = v; el.dispatchEvent(new Event("input", { bubbles: true })); } form[key] = v; };

  async function draw() {
    let st, found = [];
    try { st = await api(`${P}/status`); } catch (e) { root.replaceChildren(h("p.muted", e.message)); return; }
    try { found = await api(`${P}/discover`); } catch {}
    form.host = field("host")?.value || st.host || form.host;
    form.share = field("share")?.value || st.share || form.share;
    form.username = field("username")?.value ?? form.username;
    const pw = () => field("password")?.value || "";

    const space = Object.entries(st.folders || {}).map(([k, f]) =>
      h("div.row", h("span", LABELS[k] || k), h("span.spacer"),
        h("span.muted", f.exists === false ? "folder not found"
          : f.total_gb ? `${f.free_gb} GB free of ${f.total_gb} GB` : "-")));

    const nasList = found.length
      ? found.map((d) => h("button.chip", { onclick: () => { fill("host", d.host || d.ip); draw(); } },
          icon("nas"), " ", d.name || d.host, d.vendor ? ` (${d.vendor})` : ""))
      : [h("span.muted", "No NAS found yet (mDNS). Type its name or IP above.")];

    const shareList = shares.map((s) => h("button.chip" + (s === form.share ? ".on" : ""),
      { onclick: () => { fill("share", s); draw(); } }, s));

    root.replaceChildren(
      h("div.row", icon("nas"), h("strong", st.mounted ? "Mounted" : "Not mounted"),
        h("span.muted", " " + st.mount_point), h("span.spacer"),
        st.mounted
          ? h("button", { onclick: (e) => act(async () => { await api(`${P}/unmount`, { body: {} }); toast("Unmounted"); draw(); }, e.target) }, "Unmount")
          : h("button.primary", { onclick: (e) => act(async () => {
              await api(`${P}/mount`, { body: { host: field("host")?.value, share: field("share")?.value,
                username: field("username")?.value, password: pw() === "••••••••" ? undefined : pw(),
                mount_point: field("mount_point")?.value || undefined,
                version: field("version")?.value || undefined } });
              toast("Mounted"); draw(); }, e.target) }, "Mount")),
      st.error && !st.mounted ? h("p.muted", st.error) : null,
      !st.helper ? h("p.muted", "Mount helper missing: re-run deploy/install-pi.sh.") : null,
      h("p.muted", "Save the settings above first; Mount uses the values in the form."),
      space,
      h("h4", "NAS on your network"), h("div.row.wrap", nasList),
      h("div.row", h("button", { onclick: (e) => act(async () => {
          const r = await api(`${P}/shares`, { body: { host: field("host")?.value, username: field("username")?.value,
            password: pw(), version: field("version")?.value || undefined } });
          shares = r.shares; if (!shares.length) toast("No shares found", true); draw(); }, e.target) }, "List shares")),
      shares.length ? h("div.row.wrap", shareList) : null,
      h("h4", "Library"),
      h("button", { disabled: !st.mounted, onclick: (e) => act(async () => {
          const r = await api(`${P}/suggest`);
          const s = r.suggestions;
          if (!Object.keys(s).length) { toast("No movie or series folders found on the NAS", true); return; }
          const body = h("div.stack", Object.entries(s).map(([k, v]) => h("div.row", h("strong", LABELS[k] || k), h("span.muted", " " + v))),
            r.missing.length ? h("p.muted", "Not found: " + r.missing.join(", ")) : null);
          const sh = sheet("Use NAS for library?", [body,
            h("div.row", h("button.primary", { onclick: (ev) => act(async () => {
              await api("/api/settings/plugins/media", { method: "PUT", body: { values: s } });
              toast("Media settings updated"); sh.close(); }, ev.target) }, "Use these"),
              h("button", { onclick: () => sh.close() }, "Cancel"))]);
        }, e.target) }, "Use NAS for library"));
  }
  await draw();
}
