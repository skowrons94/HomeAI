// Shared feed panel (inside the plugin's settings sheet): status, deploy key, sync now.
import { api, h, icon, toast, act, fmt } from "../app.js";

const toSec = (ts) => (typeof ts === "number" ? ts : Date.parse(ts) / 1000);

export default async function (container, plugin) {
  const root = h("div.stack");
  container.append(root);

  async function draw(st) {
    if (!st) {
      try { st = await api("/api/p/sync/status"); }
      catch (e) { root.replaceChildren(h("p.muted", e.message)); return; }
    }
    const key = st.public_key
      ? h("div.stack", { style: { gap: "6px" } },
          h("div.small", "Deploy key (add it to your private repo):"),
          h("code", { style: { display: "block", padding: "10px", borderRadius: "8px", background: "var(--surface-2)",
            fontSize: "12px", wordBreak: "break-all", userSelect: "all" } }, st.public_key),
          h("div.row", h("button.btn.small", { onclick: async () => {
            try { await navigator.clipboard.writeText(st.public_key); toast("Copied"); }
            catch { toast("Select the key and copy it manually", true); }
          } }, icon("link"), "Copy")),
          h("p.small.muted", "GitHub → your private repo → Settings → Deploy keys → Add, tick “Allow write access”."))
      : h("p.small.muted", "The deploy key appears here once the plugin is enabled and saved.");

    const lines = [
      h("div.row", h("span", "Status"), h("span.spacer"),
        h(`span.chip${st.configured ? ".ok" : ".warn"}`, st.configured ? "Configured" : "Not configured")),
      st.user && h("div.row", h("span", "You"), h("span.spacer"), h("span.muted", st.user)),
      st.repo_url && h("div.row", h("span", "Repo"), h("span.spacer"), h("span.muted.ellipsis", st.repo_url)),
      h("div.row", h("span", "Last sync"), h("span.spacer"),
        h("span.muted", st.last_sync ? fmt.ago(toSec(st.last_sync)) : "never")),
      h("div.row", h("span", "Pending"), h("span.spacer"), h("span.muted", String(st.pending || 0))),
      st.last_error && h("p.small", { style: { color: "var(--bad, #c0392b)" } }, st.last_error),
    ];
    root.replaceChildren(...lines.filter(Boolean), key,
      h("div.row", h("button.btn.primary", { onclick: (e) => act(async () => {
        const r = await api("/api/p/sync/sync", { method: "POST", body: {} });
        toast(r.last_error ? "Sync finished with an error" : "Synced", !!r.last_error);
        await draw(r);
      }, e.currentTarget) }, icon("refresh"), "Sync now")));
  }
  await draw();
}
