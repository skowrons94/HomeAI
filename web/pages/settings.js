// Settings: every integration and core option is configured here (no config.toml editing).
//
// Custom plugin panel contract: when a plugin's settings object has `ui: true`, the plugin
// sheet loads
//   const mod = await import(`/static/plugins/${name}.js?v=${window.CASA_V}`);
//   await mod.default(container, plugin);
// where `container` is an element inside the sheet and `plugin` is the object from
// GET /api/settings (name, title, status, values, ...). Failures are swallowed (nothing shown).
//
// Exports: default page, renderForm(schema, values) -> {el, values()}, pluginSheet(plugin),
// setupWizard(plugin), folderBrowser(start) -> Promise<path|null>.
import { attentionBanner } from "./attention.js";
import { api, h, icon, sheet, store, toast, act, refreshState, bus, ensureCss } from "../app.js";

const MASK = "••••••••";
const optVal = (o) => (typeof o === "object" ? o.value : o);
const optLabel = (o) => (typeof o === "object" ? (o.label ?? o.value) : o);

// ---------------------------------------------------------------- form renderer

function switchEl(checked, onchange, disabled) {
  const input = h("input", { type: "checkbox", checked: !!checked, disabled: !!disabled, onchange });
  return { el: h("label.sw", input, h("span")), input };
}

function listEditor(value, placeholder) {
  let items = Array.isArray(value) ? [...value] : [];
  const box = h("div.st-chips");
  const input = h("input", { placeholder: placeholder || "Type and press Enter", autocomplete: "off" });
  const add = () => {
    const v = input.value.trim().replace(/,$/, "");
    if (v && !items.includes(v)) { items.push(v); draw(); }
    input.value = "";
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
    else if (e.key === "Backspace" && !input.value && items.length) { items.pop(); draw(); }
  });
  input.addEventListener("blur", add);
  function draw() {
    box.replaceChildren(...items.map((it, i) => h("span.chip", it,
      h("button", { type: "button", "aria-label": "Remove", onclick: () => { items.splice(i, 1); draw(); } }, icon("x")))), input);
  }
  draw();
  return { el: box, get: () => { add(); return [...items]; } };
}

const tag = (el, key) => [el, ...el.querySelectorAll("input,select")].forEach((i) => {
  if (/^(INPUT|SELECT)$/.test(i.tagName)) { i.name = key; i.dataset.key = key; }
});

/** Build a form from a plugin/core schema. Returns {el, values()}. */
export function renderForm(schema, values = {}) {
  const getters = {};
  const mk = (f) => {
    const cur = values[f.key] ?? f.default ?? (f.type === "list" ? [] : f.type === "bool" ? false : "");
    let control;
    switch (f.type) {
      case "bool": {
        const sw = switchEl(cur);
        getters[f.key] = () => sw.input.checked;
        tag(sw.input, f.key);
        return h("div.st-field.st-bool", h("div.st-text", h("b", f.label || f.key),
          f.help && h("span.st-help", f.help), linkEl(f)), sw.el);
      }
      case "select": {
        const sel = h("select", (f.options || []).map((o) =>
          h("option", { value: optVal(o), selected: String(optVal(o)) === String(cur) }, optLabel(o))));
        getters[f.key] = () => sel.value;
        control = sel; break;
      }
      case "number": {
        const inp = h("input", { type: "number", inputmode: "decimal", value: cur ?? "", min: f.min, max: f.max,
                                 step: f.step ?? "any", placeholder: f.placeholder });
        getters[f.key] = () => (inp.value === "" ? null : Number(inp.value));
        control = h("div.st-inline", inp, f.unit && h("span.st-unit", f.unit)); break;
      }
      case "password": {
        const inp = h("input", { type: "password", value: cur ?? "", placeholder: f.placeholder, autocomplete: "new-password" });
        const tog = h("button.ghost", { type: "button", onclick: () => {
          inp.type = inp.type === "password" ? "text" : "password"; tog.textContent = inp.type === "password" ? "Show" : "Hide";
        } }, "Show");
        getters[f.key] = () => inp.value;
        control = h("div.st-inline", inp, tog); break;
      }
      case "list": {
        const le = listEditor(cur, f.placeholder);
        getters[f.key] = le.get;
        control = le.el; break;
      }
      case "path": {
        const inp = h("input", { value: cur ?? "", placeholder: f.placeholder || "/path/to/folder", autocomplete: "off" });
        getters[f.key] = () => inp.value.trim();
        control = h("div.st-inline", inp, h("button", { type: "button", onclick: async () => {
          const p = await folderBrowser(inp.value.trim());
          if (p) inp.value = p;
        } }, icon("folder"), "Browse")); break;
      }
      default: {
        const inp = h("input", { type: "text", value: cur ?? "", placeholder: f.placeholder, autocomplete: "off" });
        getters[f.key] = () => inp.value;
        control = inp;
      }
    }
    tag(control, f.key);
    return h("div.st-field", h("div.st-label", f.label || f.key, f.required && h("span.req", "*")),
      control, (f.help || f.link) && h("div.st-help", f.help, " ", linkEl(f)));
  };
  const linkEl = (f) => f.link && h("a", { href: f.link.url, target: "_blank", rel: "noopener" }, f.link.label || "Learn more");

  const basic = schema.filter((f) => !f.advanced), adv = schema.filter((f) => f.advanced);
  const el = h("div.st-form", basic.map(mk),
    adv.length ? h("details.st-adv", h("summary", "Advanced"), h("div.st-form", adv.map(mk))) : null);
  return { el, values: () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()])) };
}

// ---------------------------------------------------------------- folder browser

/** Opens a sheet to pick a folder on the hub; resolves with the path or null. */
export function folderBrowser(start = "") {
  return new Promise((resolve) => {
    let done = false;
    const body = h("div.stack.st-fs");
    const s = sheet("Choose a folder", body, { onClose: () => { if (!done) resolve(null); } });
    const finish = (p) => { done = true; resolve(p); s.close(); };
    const load = async (path) => {
      let r;
      try { r = await api(`fs?path=${encodeURIComponent(path || "")}`); }
      catch (e) { body.replaceChildren(h("div.chip.bad", e.message)); return; }
      body.replaceChildren(
        h("div.row.wrap", { style: { gap: "6px" } }, (r.roots || []).map((p) =>
          h("button", { onclick: () => load(p) }, p.split("/").filter(Boolean).pop() || p))),
        h("div.path", r.path),
        h("div.small.muted", r.writable ? "Writable" : "Read-only", r.free_gb != null ? ` · ${r.free_gb} GB free` : ""),
        h("div.list",
          r.parent != null && h("div.item", { onclick: () => load(r.parent) }, icon("up"), h("span", "..")),
          (r.dirs || []).map((d) => h("div.item", { onclick: () => load(`${r.path.replace(/\/$/, "")}/${d}`) },
            icon("folder"), h("span.ellipsis", d), h("div.spacer"), icon("right"))),
          !(r.dirs || []).length && h("div.item.muted.small", "No subfolders")),
        h("button.primary.lg", { onclick: () => finish(r.path) }, "Use this folder"));
    };
    load(start);
  });
}

// ---------------------------------------------------------------- page

let restartNeeded = false;

function restartHub(btn) {
  return act(async () => {
    await api("restart", { body: {} }).catch(() => {});
    toast("Restarting…");
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      try { const r = await fetch("/api/state"); if (r.ok || r.status === 401) break; } catch {}
    }
    location.reload();
  }, btn);
}

function statusChip(p) {
  const st = p.status || {};
  if (!p.enabled) return h("span.chip", "off");
  if (p.error) return h("span.chip.bad", "error");
  if (st.setup && !st.configured) return h("span.chip.warn", "needs setup");
  return p.running ? h("span.chip.ok", "running") : h("span.chip.warn", "starting");
}

export default {
  async render(root) {
    ensureCss(`/static/pages/settings.css?v=${window.CASA_V}`);
    const banner = h("div.st-banner", h("b", "Restart to apply"),
      h("button.primary", { onclick: (e) => restartHub(e.currentTarget) }, "Restart"));
    const intBox = h("div.list");
    const coreBox = h("div.st-cards");
    const hiddenBox = h("div");
    root.append(
      h("header.page", h("h1", "Settings"), h("div.spacer"),
        h("button", { onclick: (e) => { if (confirm("Restart Casa now?")) restartHub(e.currentTarget); } },
          icon("power"), "Restart")),
      banner,
      attentionBanner(),
      h("div.section", h("h2", "Integrations")), intBox,
      h("div.section", h("h2", "Casa")), coreBox,
      hiddenBox);

    banner.hidden = !restartNeeded;
    const onRestart = () => { restartNeeded = true; banner.hidden = false; };
    let data = null;
    const load = async () => {
      data = await api("settings");
      intBox.replaceChildren(...data.plugins.map((p) => integrationRow(p, load)));
      if (!coreBox.children.length) {
        await refreshState();
        coreBox.replaceChildren(...data.core.map((c) => coreCard(c, onRestart)));
        const hidden = [...store.entities.values()].filter((e) => e.attrs.hidden);
        hiddenBox.replaceChildren(...(hidden.length ? [
          h("div.section", h("h2", "Hidden devices")),
          h("div.list", hidden.map((e) => h("div.item", h("span.ellipsis", e.name), h("span.muted.small", e.plugin), h("div.spacer"),
            h("button", { onclick: async (ev) => {
              await act(() => api(`entities/${encodeURIComponent(e.id)}`, { method: "PATCH", body: { hidden: false } }), ev.currentTarget);
              ev.currentTarget.closest(".item").remove();
            } }, "Show")))),
        ] : []));
      }
    };
    await load();
    const onPlugins = () => load().catch(() => {});
    bus.addEventListener("plugins", onPlugins);
    return () => bus.removeEventListener("plugins", onPlugins);
  },
};

function integrationRow(p, reload) {
  const sw = switchEl(p.enabled, () => {});
  sw.input.onchange = async () => {
    sw.input.disabled = true;
    try {
      const r = await api(`settings/plugins/${p.name}`, { method: "PUT", body: { enabled: sw.input.checked } });
      if (r.error) toast(`${p.title}: ${r.error}`, true);
      else toast(`${p.title} ${sw.input.checked ? "enabled" : "disabled"}`);
    } catch (e) { toast(e.message, true); }
    reload();
  };
  sw.el.addEventListener("click", (e) => e.stopPropagation());
  return h("div.item.st-int", { onclick: () => pluginSheet(p, reload) },
    h("div.ic", icon(p.icon || "plug")),
    h("div.txt", h("b", p.title), h("span", p.description || "")),
    statusChip(p), sw.el);
}

/** Sheet with the plugin's description, error, form, setup button and custom panel. */
export async function pluginSheet(p, reload) {
  if (typeof p === "string") {
    const d = await api("settings");
    p = d.plugins.find((x) => x.name === p);
    if (!p) return;
  }
  const st = p.status || {};
  const form = (p.schema || []).length ? renderForm(p.schema, p.values || {}) : null;
  const custom = h("div.st-custom");
  const s = sheet(p.title, [
    p.description && h("div.muted", p.description),
    h("div.row", statusChip(p)),
    p.error && h("div.chip.bad", { style: { whiteSpace: "normal" } }, p.error),
    st.setup && h("div", h("button", { onclick: () => setupWizard(p) }, icon("link"), st.configured ? "Re-pair" : "Set up")),
    form?.el,
    form && h("button.primary.lg", { onclick: (e) => act(async () => {
      const r = await api(`settings/plugins/${p.name}`, { method: "PUT", body: { values: form.values() } });
      if (r.error) toast(r.error, true); else toast("Saved");
      s.close(); reload?.(); refreshState().catch(() => {});
    }, e.currentTarget) }, "Save"),
    custom,
  ].filter(Boolean), { wide: !!p.ui });
  if (p.ui) {
    try {
      const mod = await import(`/static/plugins/${p.name}.js?v=${window.CASA_V}`);
      await mod.default(custom, p);
    } catch (e) { console.warn("plugin panel", p.name, e); custom.replaceChildren(); }
  }
}

function coreCard(c, onRestart) {
  const form = renderForm(c.schema || [], c.values || {});
  const extra = [];
  if (c.name === "notify") {
    extra.push(h("button", { onclick: (ev) => act(async () => {
      const r = await api("notifications/test", { body: {} }); toast(`Sent via ${r.channels.join(", ")}`);
    }, ev.currentTarget) }, icon("bell"), "Send test"));
  }
  if (c.name === "general") {
    extra.push(h("button", { onclick: (ev) => act(async () => {
      await api("onboarding", { body: { done: false } }); location.hash = "#/welcome";
    }, ev.currentTarget) }, icon("refresh"), "Run setup again"));
  }
  return h("div.card.stack.st-card", h("h3", c.title),
    c.name === "notify" && h("div.row.wrap", "Channels:", (store.notify_channels || []).map((n) => h("span.chip.ok", n))),
    form.el,
    h("div.st-actions",
      h("button.primary", { onclick: (ev) => act(async () => {
        const r = await api(`settings/core/${c.name}`, { method: "PUT", body: { values: form.values() } });
        toast("Saved"); if (r.restart) onRestart();
        refreshState().catch(() => {});
      }, ev.currentTarget) }, "Save"), extra),
    c.name === "homekit" && homekitCard());
}

function homekitCard() {
  const box = h("div");
  api("homekit").then((hk) => {
    if (!hk || !hk.running) { box.append(h("div.muted.small", "HomeKit bridge is not running.")); return; }
    box.append(h("div.row.wrap", { style: { alignItems: "flex-start", gap: "20px" } },
      h("img", { src: "/api/homekit/qr.svg", width: 180, height: 180,
                 style: { background: "#fff", borderRadius: "12px" }, alt: "HomeKit QR" }),
      h("div.stack", { style: { flex: 1, minWidth: "200px" } },
        h("div.row", h("b", hk.name), hk.paired ? h("span.chip.ok", "paired") : h("span.chip.warn", "not paired")),
        h("div", "Setup code ", h("b", { style: { fontSize: "20px", letterSpacing: ".05em" } }, hk.pincode)),
        h("div.small.muted", "Home app → + → Add Accessory → scan the code, accept “Uncertified Accessory”."),
        h("div.row.wrap", { style: { gap: "4px" } }, (hk.accessories || []).map((a) => h("span.chip", a))))));
  }).catch(() => {});
  return box;
}

/** Generic setup flow: POST /api/p/<name>/setup {step, value} until done. */
export async function setupWizard(p) {
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
      r.done ? h("button.primary.lg", { onclick: () => { s.close(); refreshState(); bus.dispatchEvent(new CustomEvent("plugins")); } }, "Done")
             : !r.input && !r.choices && h("button.primary.lg", { onclick: () => run() }, r.error ? "Try again" : "Continue"),
    ].filter(Boolean));
    input?.focus();
  };
  run();
}
