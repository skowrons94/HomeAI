// Welcome: guided first-run setup. Every step is skippable and everything stays editable in Settings.
// Reuses renderForm / pluginSheet / setupWizard from settings.js. Current step is kept in
// sessionStorage so a reload resumes where the user was.
import { api, h, icon, toast, act, store, refreshState, ensureCss, t } from "../app.js";
import { renderForm, pluginSheet, setupWizard } from "./settings.js";

const STEPS = ["welcome", "discover", "integrations", "configure", "notify", "homekit", "done"];
const LABELS = { welcome: "Welcome", discover: "Discover", integrations: "Integrations",
                 configure: "Configure", notify: "Notifications", homekit: "Apple Home", done: "Done" };
const KEY = "casa.welcome";
const ALWAYS = ["system", "media", "recipes", "bluetooth", "network"];
const KIND = { apple_tv: "Apple TV", speaker: "Speaker", tv: "TV", nas: "NAS", ikea_hub: "IKEA hub", router: "Router",
               computer: "Computer", cast: "Cast device", printer: "Printer", hub: "Hub", home_assistant: "Home Assistant",
               homekit: "HomeKit device", light: "Light", sensor: "Sensor", iot: "Smart device", homepod: "HomePod",
               boiler: "Boiler", device: "Device" };

const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY)) || {}; } catch { return {}; } };
const S = load();   // {step, qi, selected:[...], saved:{name:{running,error}}, restart}
const persist = () => { try { sessionStorage.setItem(KEY, JSON.stringify(S)); } catch {} };
const put = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((x) => x));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let data = null;       // GET /api/settings
let devices = [];      // discovered
let root, body, foot, dots, alive;

const plugin = (n) => data.plugins.find((p) => p.name === n);
const core = (n) => data.core.find((c) => c.name === n);
const basicFields = (p) => (p.schema || []).filter((f) => !f.advanced)
  .sort((a, b) => !!b.required - !!a.required);
const suggested = () => new Set(devices.map((d) => d.suggested_plugin).filter(Boolean));
const hasNas = () => devices.some((d) => d.kind === "nas" || (d.services || []).some((s) => /smb|afpover/.test(s)));
async function reloadSettings() { data = await api("settings"); }

// ---------------------------------------------------------------- shell

function go(step) {
  S.step = step; persist();
  draw();
}

async function finish(dest = "#/home") {
  try { await api("onboarding", { body: { done: true } }); } catch (e) { toast(e.message, true); return; }
  store.onboarding = false;
  try { sessionStorage.removeItem(KEY); } catch {}
  const needRestart = S.restart;
  for (const k of Object.keys(S)) delete S[k];
  if (needRestart) await restartHub();
  location.hash = dest;
  if (!needRestart) refreshState().catch(() => {});
}

async function restartHub() {
  body.replaceChildren(h("div.wl-center", h("span.spin"), h("div", t("Restarting Casa…")),
    h("div.muted.small", t("This takes a few seconds."))));
  foot.replaceChildren();
  await api("restart", { body: {} }).catch(() => {});
  await sleep(1500);
  for (let i = 0; i < 90; i++) {
    try { const r = await fetch("/api/state"); if (r.ok || r.status === 401) break; } catch {}
    await sleep(1000);
  }
  try { sessionStorage.removeItem(KEY); } catch {}
  location.hash = "#/home";
  location.reload();
}

function nav({ back = true, next = t("Continue"), onNext, skip = true, nextDisabled = false } = {}) {
  const i = STEPS.indexOf(S.step);
  const goNext = () => go(STEPS[i + 1]);
  const goBack = () => go(STEPS[i - 1]);
  put(foot, 
    back && i > 0 ? h("button.ghost", { onclick: goBack }, icon("left"), t("Back")) : h("span"),
    h("div.spacer"),
    skip && h("button.ghost", { onclick: goNext }, t("Skip")),
    h("button.primary.lg", { disabled: nextDisabled, onclick: (e) => act(async () => {
      if (onNext) { const ok = await onNext(); if (ok === false) return; }
      goNext();
    }, e.currentTarget) }, next));
}

function card(title, sub, ...content) {
  return [h("h1.wl-title", title), sub && h("p.wl-sub", sub), ...content].filter((x) => x);
}

function draw() {
  if (!alive) return;
  const i = STEPS.indexOf(S.step);
  dots.replaceChildren(...STEPS.map((s, k) => h("span.wl-dot", {
    class: k < i ? "done" : k === i ? "cur" : "", title: t(LABELS[s]) })));
  root.querySelector(".wl-count").textContent = t("Step {n} of {total} · {name}", { n: i + 1, total: STEPS.length, name: t(LABELS[S.step]) });
  body.replaceChildren(); foot.replaceChildren();
  root.querySelector(".wl-card").scrollTop = 0; window.scrollTo(0, 0);
  steps[S.step]().catch((e) => { body.replaceChildren(h("div.chip.bad", e.message)); nav(); });
}

// ---------------------------------------------------------------- steps

const steps = {
  async welcome() {
    const g = core("general"), w = core("web");
    const name = h("input", { value: g.values.name || "", placeholder: "Casa", autocomplete: "off" });
    const rooms = renderForm(g.schema.filter((f) => f.key === "rooms"), g.values);
    const pw = h("input", { type: "password", placeholder: t("Optional"), autocomplete: "new-password" });
    body.replaceChildren(...card(t("Welcome home"),
      t("Casa brings your lights, heating, speakers, TV, media and recipes together in one calm place, and runs quietly on your own network. This short setup connects what you have; every step can be skipped and everything can be changed later in Settings."),
      h("div.st-form",
        h("div.st-field", h("div.st-label", t("Name of your home")), name),
        rooms.el,
        h("div.st-field", h("div.st-label", t("App password")), pw,
          h("div.st-help", t("Optional. Leave empty to open the app without a password, which is fine on a home network."))))));
    nav({ back: false, skip: true, next: t("Continue"), onNext: async () => {
      await api("settings/core/general", { method: "PUT", body: { values: { name: name.value.trim() || "Casa", ...rooms.values() } } });
      if (pw.value) {
        const r = await api("settings/core/web", { method: "PUT", body: { values: { password: pw.value } } });
        if (r.restart) { S.restart = true; persist(); }
      }
      await refreshState().catch(() => {});
    } });
  },

  async discover() {
    const status = h("div.row", h("span.spin"), h("span", t("Starting the network scan…")));
    const list = h("div.stack");
    body.replaceChildren(...card(t("Looking around"),
      t("Casa scans your home network for speakers, TVs, hubs and storage it knows how to talk to."), status, list));
    nav({ nextDisabled: false });
    const r = await api("settings/plugins/network", { method: "PUT", body: { enabled: true } }).catch((e) => ({ error: e.message }));
    if (r.error) { status.replaceChildren(h("div.chip.bad", { style: { whiteSpace: "normal" } }, t("Scan unavailable: {error}", { error: r.error })),
                   h("div.muted.small", t("You can continue without it."))); return; }
    const t0 = Date.now();
    while (alive && S.step === "discover") {
      try { devices = await api("/api/p/network/devices"); } catch {}
      const found = devices.filter((d) => d.online !== false);
      const known = found.filter((d) => d.suggested_plugin);
      const others = found.length - known.length;
      put(list, 
        known.length ? h("div.section.wl-sec", h("h2", t("We found these"))) : null,
        known.length ? h("div.list", known.map(deviceRow)) : null,
        others ? h("div.muted.small", others > 1 ? t("{n} other devices on your network (not supported yet).", { n: others }) : t("1 other device on your network (not supported yet).")) : null);
      const done = Date.now() - t0 > 15000;
      status.replaceChildren(...[done ? h("span", known.length ? (known.length > 1 ? t("Found {n} devices Casa can use.", { n: known.length }) : t("Found 1 device Casa can use."))
                                                         : t("Nothing recognised yet. You can add integrations by hand next."))
        : [h("span.spin"), h("span", known.length ? t("Found {n} so far…", { n: known.length }) : t("Scanning…"))]].flat());
      status.classList.toggle("wl-done", done);
      if (done) break;
      await sleep(2000);
    }
  },

  async integrations() {
    await reloadSettings();
    if (!S.selected) {
      const sg = suggested(), sel = new Set(ALWAYS);
      if (hasNas()) sel.add("storage");
      sg.forEach((n) => sel.add(n));
      data.plugins.filter((p) => p.enabled).forEach((p) => sel.add(p.name));
      S.selected = [...sel].filter((n) => plugin(n)); persist();
    }
    const sg = suggested();
    const rows = data.plugins.map((p) => {
      const input = h("input", { type: "checkbox", checked: S.selected.includes(p.name), onchange: () => {
        S.selected = input.checked ? [...new Set([...S.selected, p.name])] : S.selected.filter((n) => n !== p.name); persist();
      } });
      const found = devices.find((d) => d.suggested_plugin === p.name);
      return h("div.item.st-int.wl-int", h("div.ic", icon(p.icon || "plug")),
        h("div.txt", h("b", t(p.title), found && h("span.chip.accent", { style: { marginLeft: "8px" } }, t("found"))),
          h("span.wl-wrap", p.description ? t(p.description) : "")),
        h("label.sw", input, h("span")));
    });
    body.replaceChildren(...card(t("What should Casa connect to?"),
      sg.size ? t("We pre-selected what we found on your network plus a few always-useful basics.") : t("Pick the integrations you want. You can change this any time."),
      h("div.list", rows)));
    nav({ onNext: async () => {
      // apply on/off: enable new, disable deselected (also removes the scan if you opt out)
      for (const p of data.plugins) {
        const want = S.selected.includes(p.name);
        if (want === !!p.enabled) continue;
        try { await api(`settings/plugins/${p.name}`, { method: "PUT", body: { enabled: want } }); }
        catch (e) { toast(`${t(p.title)}: ${e.message}`, true); }
      }
      await reloadSettings();
      S.qi = 0; persist();
    } });
  },

  async configure() {
    await reloadSettings();
    const queue = (S.selected || []).map(plugin).filter((p) => p && (basicFields(p).length || p.ui || p.status?.setup));
    if (!queue.length) { body.replaceChildren(...card(t("Nothing to configure"), t("The integrations you picked work without further settings."))); return nav({ skip: false }); }
    S.qi = Math.min(S.qi || 0, queue.length - 1);
    const p = queue[S.qi], last = S.qi === queue.length - 1;
    const fields = basicFields(p);
    const vals = { ...(p.values || {}) };
    const dev = devices.find((d) => d.suggested_plugin === p.name);
    if (dev && fields.some((f) => f.key === "host") && !vals.host) vals.host = dev.ip;
    const form = fields.length ? renderForm(fields, vals) : null;
    const result = h("div.stack");
    const showResult = (r) => {
      const st = r.status || {};
      put(result, 
        r.error ? h("div.chip.bad", { style: { whiteSpace: "normal" } }, r.error) : h("span.chip.ok", r.running ? t("Connected and running") : t("Saved")),
        r.error && h("div.muted.small", t("You can fix this later in Settings.")),
        st.setup && !st.configured && h("div.card.stack.wl-setup", h("b", t("One more step: pair it")),
          h("div.small.muted", t("This device needs a one-time pairing.")),
          h("button.primary", { onclick: () => setupWizard(p) }, icon("link"), t("Start pairing"))));
    };
    const saved = S.saved?.[p.name];
    body.replaceChildren(...card(t("{n} of {total}: {name}", { n: S.qi + 1, total: queue.length, name: t(p.title) }), p.description && t(p.description),
      dev && fields.some((f) => f.key === "host") && h("div.chip.accent", { style: { alignSelf: "flex-start" } }, t("Found at {ip}", { ip: dev.ip })),
      form?.el,
      !form && h("div.muted", t("No settings needed here.")),
      p.ui && h("div", h("button", { onclick: () => pluginSheet(p.name) }, icon("settings"), t("More options"))),
      result));
    if (saved) showResult(saved);
    const advance = () => { if (last) go("notify"); else { S.qi++; persist(); draw(); } };
    const i = STEPS.indexOf("configure");
    put(foot, 
      h("button.ghost", { onclick: () => { if (S.qi > 0) { S.qi--; persist(); draw(); } else go(STEPS[i - 1]); } }, icon("left"), t("Back")),
      h("div.spacer"),
      h("button.ghost", { onclick: advance }, t("Skip")),
      h("button.primary.lg", { onclick: (e) => act(async () => {
        const r = await api(`settings/plugins/${p.name}`, { method: "PUT", body: { enabled: true, ...(form ? { values: form.values() } : {}) } })
          .catch((er) => ({ error: er.message }));
        await reloadSettings();
        const np = plugin(p.name);
        const res = { running: r.running, error: r.error || np?.error, status: np?.status };
        (S.saved ||= {})[p.name] = { running: res.running, error: res.error }; persist();
        showResult(res);
        const needsPair = np?.status?.setup && !np.status.configured;
        if (!res.error && !needsPair) { await sleep(700); advance(); }
        else foot.querySelector("button.primary").textContent = last ? t("Next step") : t("Next");
        if (res.error || needsPair) foot.querySelector("button.primary").onclick = advance;
      }, e.currentTarget) }, form ? t("Save & continue") : t("Enable")));
  },

  async notify() {
    const c = core("notify");
    const form = renderForm(c.schema.filter((f) => f.key === "ntfy_url"), c.values);
    body.replaceChildren(...card(t("Notifications"),
      t("Casa can send alerts to your phone, for example when a download finishes or the boiler needs attention. The easiest way is ntfy: install the free ntfy app, subscribe to a topic with a hard-to-guess name, then paste its address here."),
      form.el,
      h("div.row.wrap", h("button", { onclick: (e) => act(async () => {
        await api("settings/core/notify", { method: "PUT", body: { values: form.values() } });
        const r = await api("notifications/test", { body: {} });
        toast(r.channels?.length ? t("Test sent via {channels}", { channels: r.channels.join(", ") }) : t("No channel configured yet"), !r.channels?.length);
      }, e.currentTarget) }, icon("bell"), t("Send test")))));
    nav({ onNext: async () => {
      const v = form.values();
      if (v.ntfy_url?.trim()) await api("settings/core/notify", { method: "PUT", body: { values: v } });
    } });
  },

  async homekit() {
    const hk = await api("homekit").catch(() => null);
    if (!hk || !hk.running) {
      body.replaceChildren(...card(t("Apple Home"), t("The HomeKit bridge is not running, so there is nothing to add yet. You can turn it on later in Settings.")));
      return nav({ skip: false });
    }
    body.replaceChildren(...card(t("Add Casa to Apple Home"),
      t("Your lights, thermostats and sensors can appear in the Apple Home app, Siri and Control Center."),
      h("div.wl-hk",
        h("img.wl-qr", { src: "/api/homekit/qr.svg", alt: t("HomeKit setup QR code"), width: 180, height: 180 }),
        h("div.stack", h("div.muted.small", t("Setup code")), h("div.wl-code", hk.pincode),
          hk.paired && h("span.chip.ok", t("already paired")))),
      h("ol.wl-steps",
        h("li", t("Open the Home app on your iPhone.")),
        h("li", t("Tap + then Add Accessory and scan this code.")),
        h("li", t("Accept Uncertified Accessory and pick a room.")))));
    nav({ next: t("Continue"), skip: false });
  },

  async done() {
    await reloadSettings();
    await refreshState().catch(() => {});
    const on = data.plugins.filter((p) => p.enabled);
    const row = (p) => h("div.item.st-int", h("div.ic", icon(p.icon || "plug")),
      h("div.txt", h("b", t(p.title))),
      p.error ? h("span.chip.bad", t("needs attention")) : p.status?.setup && !p.status.configured ? h("span.chip.warn", t("needs pairing"))
        : h("span.chip.ok", p.running ? t("running") : t("on")));
    body.replaceChildren(...card(t("You are all set"), t("{name} is ready. Everything you chose here can be changed any time in Settings.", { name: store.name || "Casa" }),
      on.length ? h("div.list", on.map(row)) : h("div.muted", t("No integrations enabled yet. Add some from Settings.")),
      S.restart && h("div.chip.warn", { style: { whiteSpace: "normal" } }, t("Casa will restart once to apply your password."))));
    put(foot, 
      h("button.ghost", { onclick: () => go("homekit") }, icon("left"), t("Back")), h("div.spacer"),
      h("button.primary.lg", { onclick: (e) => act(() => finish(), e.currentTarget) }, t("Go to Home")));
  },
};

// ---------------------------------------------------------------- page

function deviceRow(d) {
  const p = plugin(d.suggested_plugin);
  return h("div.item.st-int", h("div.ic", icon(p?.icon || "plug")),
    h("div.txt", h("b", d.name || (KIND[d.kind] && t(KIND[d.kind])) || d.ip),
      h("span", `${t(KIND[d.kind] || "Device")} · ${d.ip}`)),
    h("span.chip.accent", p ? t(p.title) : d.suggested_plugin));
}

export default {
  async render(el) {
    ensureCss(`/static/pages/settings.css?v=${window.CASA_V}`);
    ensureCss(`/static/pages/welcome.css?v=${window.CASA_V}`);
    alive = true;
    try { if (!sessionStorage.getItem(KEY)) for (const k of Object.keys(S)) delete S[k]; } catch {}
    document.body.classList.add("welcome-mode");
    dots = h("div.wl-dots");
    body = h("div.wl-body");
    foot = h("div.wl-foot");
    root = h("div.wl",
      h("div.wl-top", h("div.wl-brand", icon("home"), "Casa"), h("div.spacer"),
        h("button.ghost.wl-skipall", { onclick: () => finish() }, t("Skip setup"))),
      dots, h("div.wl-count.muted.small"),
      h("div.wl-card", body), foot);
    el.replaceChildren(root);
    try {
      await reloadSettings();
      devices = await api("/api/p/network/devices").catch(() => []);
      if (!Array.isArray(devices)) devices = [];
    } catch (e) { body.replaceChildren(h("div.chip.bad", e.message)); return; }
    if (!STEPS.includes(S.step)) S.step = "welcome";
    draw();
    return () => { alive = false; document.body.classList.remove("welcome-mode"); };
  },
};
