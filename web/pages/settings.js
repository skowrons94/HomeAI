// Settings: plugin status + setup wizards, HomeKit pairing, notifications, hidden devices.
import { api, h, icon, sheet, store, toast, act, refreshState, fmt } from "../app.js";

export default {
  async render(root) {
    await refreshState();
    const hk = store.homekit || {};
    const hidden = [...store.entities.values()].filter((e) => e.attrs.hidden);

    root.append(
      h("header.page", h("h1", "Settings")),

      h("div.section", h("h2", "Plugins")),
      h("div.list", store.plugins.map(pluginRow)),
      h("p.small.muted", "Plugins are enabled with a ", h("code", "[plugins.<name>]"),
        " section in config.toml — restart Casa after editing."),

      h("div.section", h("h2", "Apple Home")),
      h("div.card.row.wrap", { style: { alignItems: "flex-start", gap: "20px" } },
        hk.running ? [
          h("div.qr", { html: "" }, h("img", { src: "/api/homekit/qr.svg", width: 180, height: 180,
                                               style: { background: "#fff", borderRadius: "12px" }, alt: "HomeKit QR" })),
          h("div.stack", { style: { flex: 1, minWidth: "220px" } },
            h("div.row", h("b", hk.name), hk.paired ? h("span.chip.ok", "paired") : h("span.chip.warn", "not paired")),
            h("div", "Setup code ", h("b", { style: { fontSize: "20px", letterSpacing: ".05em" } }, hk.pincode)),
            h("div.small.muted", "Home app → + → Add Accessory → scan the code, accept “Uncertified Accessory”."),
            h("div.row.wrap", { style: { gap: "4px" } }, (hk.accessories || []).map((a) => h("span.chip", a)))),
        ] : h("div.muted", "HomeKit bridge is disabled.")),

      h("div.section", h("h2", "Notifications")),
      h("div.card.stack",
        h("div.row.wrap", "Channels:", (store.notify_channels || []).map((c) => h("span.chip.ok", c))),
        !(store.notify_channels || []).includes("ntfy") && h("div.small.muted",
          "Get push notifications on your phone: install the free ",
          h("a", { href: "https://ntfy.sh", target: "_blank" }, "ntfy"),
          " app, subscribe to a hard-to-guess topic and set ", h("code", "ntfy_url"), " under ",
          h("code", "[notify]"), " in config.toml."),
        h("div", h("button", { onclick: (ev) => act(async () => {
          const r = await api("notifications/test", { body: {} }); toast(`Sent via ${r.channels.join(", ")}`);
        }, ev.currentTarget) }, icon("bell"), "Send test"))),

      ...(hidden.length ? [
        h("div.section", h("h2", "Hidden devices")),
        h("div.list", hidden.map((e) => h("div.item", h("span.ellipsis", e.name), h("span.muted.small", e.plugin), h("div.spacer"),
          h("button", { onclick: async (ev) => {
            await act(() => api(`entities/${encodeURIComponent(e.id)}`, { method: "PATCH", body: { hidden: false } }), ev.currentTarget);
            ev.currentTarget.closest(".item").remove();
          } }, "Show")))),
      ] : []),
    );
  },
};

function pluginRow(p) {
  const st = p.status || {};
  const details = Object.entries(st).filter(([k, v]) => !["setup", "configured", "error"].includes(k)
                                                     && v !== null && typeof v !== "object");
  return h("div.item", { style: { alignItems: "flex-start" } },
    h("div.stack", { style: { gap: "4px", flex: 1, minWidth: 0 } },
      h("div.row", h("b", p.title),
        p.error ? h("span.chip.bad", "error") : st.setup && !st.configured ? h("span.chip.warn", "needs setup")
                : h("span.chip.ok", "running")),
      p.error && h("div.small", { style: { color: "var(--bad)" } }, p.error),
      details.length ? h("div.small.muted", details.map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")) : null),
    st.setup && h("button", { onclick: () => setupWizard(p) }, st.configured ? "Re-pair" : "Set up"));
}

/** Generic setup flow: POST /api/p/<name>/setup {step, value} until done. */
async function setupWizard(p) {
  const body = h("div.stack");
  const s = sheet(`Set up ${p.title}`, body);
  let step = null;
  const run = async (value = null) => {
    body.replaceChildren(h("div.row", h("span.spin"), h("span.muted", "Working…")));
    let r;
    try { r = await api(`/api/p/${p.name}/setup`, { body: { step, value } }); }
    catch (e) { r = { error: e.message, message: "", step }; }
    step = r.step ?? null;
    const input = r.input && h("input", { type: r.input.type || "text", placeholder: r.input.label,
                                          autocomplete: "off", inputmode: r.input.type === "number" ? "numeric" : undefined });
    body.replaceChildren(...[
      r.error && h("div.chip.bad", { style: { whiteSpace: "normal" } }, r.error),
      r.message && h("div", { style: { whiteSpace: "pre-line" } }, r.message),
      r.choices && h("div.list", r.choices.map((c) => h("div.item", { style: { cursor: "pointer" }, onclick: () => run(c.value) },
        h("span", c.label), h("div.spacer"), icon("right")))),
      input && h("form.stack", { onsubmit: (e) => { e.preventDefault(); run(input.value.trim()); } },
        input, h("button.primary.lg", { type: "submit" }, "Continue")),
      r.done ? h("button.primary.lg", { onclick: () => { s.close(); refreshState(); location.reload(); } }, "Done")
             : !r.input && !r.choices && h("button.primary.lg", { onclick: () => run() }, r.error ? "Try again" : "Continue"),
    ].filter(Boolean));
    input?.focus();
  };
  run();
}
