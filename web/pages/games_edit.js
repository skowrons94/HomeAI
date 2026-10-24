// Games: the sheets that change things — edit a game's details and pictures, delete files, choose where a game is
// installed on the Steam Deck, remove a download (keep or delete the files). Loaded on demand by games.js
// (hub/plugins/games/). `ctx` = { P, refresh, data(), gtitle(g), cover(g, cls), fixMatch(g), reload() }.
import { h, icon, toast, act, fmt, t, sheet, errorCard } from "../app.js";

const enc = encodeURIComponent;

// ---- Deck locations ---------------------------------------------------------------------------

let locCache = { at: 0, data: null };

/** Where the Deck can install games ({supported, locations:[{id,kind,label,path,free,total}], …}); cached 20 s. */
export async function loadLocations(ctx, fresh = false) {
  if (!fresh && locCache.data && Date.now() - locCache.at < 20000) return locCache.data;
  const d = await ctx.P(`deck/locations${fresh ? "?fresh=1" : ""}`);
  locCache = { at: Date.now(), data: d };
  return d;
}

export const locLabel = (d, path) => {
  if (!path) return t("Internal storage");
  const hit = (d?.locations || []).find((l) => l.path === path);
  if (hit) return hit.kind === "sd" ? (hit.label ? t("SD card {name}", { name: hit.label }) : t("SD card")) : t("Internal storage");
  return path;
};

const freeLine = (l) => (l.total ? t("{free} free of {total}", { free: fmt.bytes(l.free), total: fmt.bytes(l.total) }) : "");

function spaceBar(l) {
  const used = l.total ? Math.min(100, Math.max(0, Math.round((1 - l.free / l.total) * 100))) : 0;
  return h("div.progress.gm-space", h("div", { style: { width: `${used}%` } }));
}

/** Choose a folder on the Deck. onPick(path) gets "" for the default (internal storage). `need` = bytes the game
 *  takes, to warn about free space. */
export function chooseLocation(ctx, { title, current = "", need = 0, withDefault = false, onPick }) {
  const body = h("div.stack");
  const s = sheet(title || t("Where to install"), [body]);
  const pick = (p) => { onPick(p); s.close(); };
  const draw = (d) => {
    if (!d.online) {
      body.replaceChildren(errorCard(d.error || t("The Steam Deck is not reachable"), { title: t("Steam Deck offline"), retry: load }));
      return;
    }
    if (!d.supported) {
      body.replaceChildren(h("div.gm-banner", icon("download"), h("span", d.error || t("Update the Deck agent to choose where games are installed")),
        h("span.gm-note", t("Run deploy/deck/install-deck.sh again on the Deck, then reopen this."))));
      return;
    }
    const rows = d.locations.map((l) => {
      const on = (current || d.default) === l.path || (!current && l.kind === "internal");
      const small = need && l.free < need * 1.02 + (50 << 20);
      return h("button.gm-opt.gm-loc" + (on ? ".on" : ""), { disabled: !l.writable, onclick: () => pick(withDefault && l.kind === "internal" ? "" : l.path) },
        icon(l.kind === "sd" ? "storage" : "cpu"),
        h("span.gm-loc-t", h("b", l.kind === "sd" ? (l.label ? t("SD card {name}", { name: l.label }) : t("SD card")) : t("Internal storage")),
          h("span.gm-meta", l.path), spaceBar(l),
          h("span.gm-meta" + (small ? ".gm-bad" : ""), small ? t("Not enough space: needs {size}", { size: fmt.bytes(need) }) + " · " + freeLine(l) : freeLine(l))),
        on ? icon("check") : null);
    });
    const input = h("input", { type: "text", placeholder: d.default || "/home/deck/Games", value: current && !d.locations.some((l) => l.path === current) ? current : "",
      autocomplete: "off", autocapitalize: "off", spellcheck: false, "aria-label": t("Custom folder") });
    const note = h("div.gm-note");
    const check = async (btn) => {
      const p = input.value.trim();
      if (!p) return;
      note.className = "gm-note"; note.textContent = "";
      try {
        const r = await ctx.P("deck/location/check", { body: { path: p } });
        note.textContent = t("OK: {path} · {free} free", { path: r.path, free: fmt.bytes(r.free) });
        if (need && r.free < need * 1.02 + (50 << 20)) { note.className = "gm-note err"; note.textContent = t("Not enough space: needs {size}", { size: fmt.bytes(need) }) + " · " + fmt.bytes(r.free); }
        else pick(r.path);
      } catch (e) { note.className = "gm-note err"; note.textContent = e.message; }
      btn && (btn.disabled = false);
    };
    body.replaceChildren(...[
      withDefault ? h("div.gm-note", t("Used for every PC game unless you pick another place for a game.")) : null,
      h("div.gm-list", rows),
      h("div.gm-custom", h("div.gm-sublabel", t("Custom folder")),
        h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); check(); } }, input,
          h("button", { type: "submit" }, t("Use"))),
        h("div.gm-note", t("Only folders in {home} or on an SD card are accepted.", { home: d.home || "/home/deck" })), note),
      h("div.gm-note", t("ROMs always go to the Emulation folder.")),
    ].filter(Boolean));
  };
  const load = async () => {
    body.replaceChildren(h("div.empty", h("span.spin")));
    try { draw(await loadLocations(ctx, true)); }
    catch (e) { body.replaceChildren(errorCard(e.message, { retry: load })); }
  };
  load();
  return s;
}

// ---- pictures -----------------------------------------------------------------------------------

function pickImage(ctx, g, kind, done) {
  const cover = kind === "cover";
  const grid = h("div.gm-artgrid" + (cover ? ".cover" : ".hero"));
  const msg = h("div.gm-note");
  const url = h("input", { type: "url", placeholder: "https://…", autocomplete: "off", autocapitalize: "off", spellcheck: false,
    inputmode: "url", "aria-label": t("Image address") });
  const use = (body, btn) => act(async () => {
    await ctx.P("meta/image", { body: { id: g.id, kind, ...body } });
    toast(t("Picture updated")); s.close(); done();
  }, btn);
  const s = sheet(cover ? t("Choose a cover") : t("Choose a banner"), [
    h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); if (url.value.trim()) use({ url: url.value.trim() }, e.submitter); } },
      url, h("button.primary", { type: "submit" }, t("Use"))),
    h("div.gm-note", t("Paste the address of an image (https only). It is downloaded and saved on the hub.")),
    msg, grid,
    g.meta?.edited?.includes(kind) ? h("button.ghost", { onclick: (e) => use({ clear: true }, e.currentTarget) }, icon("refresh"), t("Back to the automatic picture")) : null,
  ]);
  const load = async () => {
    grid.replaceChildren(...Array.from({ length: 6 }, () => h("div.gm-artopt.gm-skel")));
    msg.textContent = ""; msg.className = "gm-note";
    try {
      const d = await ctx.P(`meta/art?id=${enc(g.id)}&kind=${kind}`);
      const opts = d.options || [];
      if (!opts.length) { grid.replaceChildren(); msg.textContent = d.steamgriddb ? t("No alternatives found. Paste an image address above.") : t("Add a SteamGridDB key in Settings to see more pictures, or paste an address."); return; }
      grid.replaceChildren(...opts.map((o) => h("button.gm-artopt", { "aria-label": t("Use this picture"),
        onclick: (e) => use(o.cached ? { cached: o.cached } : { url: o.url }, e.currentTarget) },
      (() => { const im = h("img", { src: o.thumb, loading: "lazy", alt: "", referrerpolicy: "no-referrer" });
        im.addEventListener("error", () => im.closest(".gm-artopt")?.remove(), { once: true }); return im; })(),
      o.w && o.h ? h("span.gm-artdim", `${o.w}×${o.h}`) : null)));
    } catch (e) {
      grid.replaceChildren();
      if (/SteamGridDB/.test(e.message)) { msg.textContent = e.message; return; }   // a hint, not a failure
      grid.replaceWith(errorCard(e.message, { retry: () => { load(); } }));
    }
  };
  load();
}

// ---- edit a game --------------------------------------------------------------------------------

export function editGame(ctx, g) {
  const m = g.meta || {};
  const edited = new Set(m.edited || []);
  const tag = (k) => (edited.has(k) ? h("span.chip.accent.gm-edited", t("edited")) : null);
  const title = h("input", { type: "text", value: ctx.gtitle(g), maxlength: 150, autocomplete: "off" });
  const year = h("input", { type: "number", inputmode: "numeric", min: 1950, max: 2100, placeholder: "2020",
    value: (m.released || "").slice(0, 4) });
  const genres = h("input", { type: "text", value: (m.genres || []).join(", "), placeholder: t("Horror, Indie, Co-op"), autocomplete: "off" });
  const summary = h("textarea", { rows: 5, maxlength: 4000, placeholder: t("Description") }, m.summary || "");
  const imgRow = (kind, label, url) => h("div.gm-imgrow",
    h("div.gm-imgprev" + (kind === "cover" ? ".cover" : ".hero"), url ? (() => { const i = h("img", { src: url, alt: "" }); i.addEventListener("error", () => i.remove(), { once: true }); return i; })() : null),
    h("div.gm-imginfo", h("b", label, tag(kind)), h("span.gm-meta", kind === "cover" ? t("Also used on the Steam Deck") : t("Shown at the top of the game"))),
    h("button", { onclick: () => pickImage(ctx, g, kind, () => { s.close(); ctx.refresh().then(() => ctx.reopenEdit?.(g.id)); }) }, icon("edit"), t("Change")));
  const rev = m.rev || 0;
  const save = (btn) => act(async () => {
    const body = { id: g.id };
    const cur = { title: ctx.gtitle(g), year: (m.released || "").slice(0, 4), genres: (m.genres || []).join(", "), summary: m.summary || "" };
    if (title.value.trim() !== cur.title) body.title = title.value.trim();
    if (year.value.trim() !== cur.year) body.year = year.value.trim();
    if (genres.value.trim() !== cur.genres) body.genres = genres.value.split(/[,;]/).map((x) => x.trim()).filter(Boolean);
    if (summary.value.trim() !== cur.summary.trim()) body.summary = summary.value.trim();
    if (Object.keys(body).length > 1) await ctx.P("meta/override", { body });
    toast(t("Saved")); s.close(); await ctx.refresh();
  }, btn);
  const reset = (btn) => confirm(t("Remove all edits for {title} and look the game up again?", { title: ctx.gtitle(g) })) && act(async () => {
    await ctx.P("meta/reset", { body: { id: g.id } });
    toast(t("Back to automatic")); s.close(); await ctx.refresh();
  }, btn);
  const s = sheet(t("Edit details"), [
    h("div.gm-note", t("Your changes are kept when the details are refreshed. Empty a field to go back to the automatic value.")),
    h("label.field", h("span", t("Title"), tag("title")), title),
    h("div.gm-two",
      h("label.field", h("span", t("Year"), tag("year")), year),
      h("label.field", h("span", t("Genres"), tag("genres")), genres)),
    h("label.field", h("span", t("Description"), tag("summary")), summary),
    h("div.gm-sublabel", t("Pictures")),
    imgRow("cover", t("Cover"), g.cover ? `/api/p/games/cover/${enc(g.id)}?v=${rev}` : ""),
    imgRow("hero", t("Banner"), m.has_hero ? `/api/p/games/hero/${enc(g.id)}?v=${rev}` : ""),
    h("button.primary.lg", { onclick: (e) => save(e.currentTarget) }, icon("check"), t("Save")),
    h("div.gm-sublabel", t("Wrong game?")),
    h("button", { onclick: () => { s.close(); ctx.fixMatch(g); } }, icon("search"), t("Match to another game")),
    h("button.danger", { onclick: (e) => reset(e.currentTarget) }, icon("refresh"), t("Reset to automatic")),
  ]);
  s.el.parentElement.classList.add("gm-esheet");
  return s;
}

// ---- delete a game from the library ---------------------------------------------------------------

export function deleteGame(ctx, g, onDone) {
  const onDeck = g.state === "installed" || g.state === "update";
  const s = sheet(t("Delete game"), [
    h("div.gm-dtitle", { style: { fontSize: "17px" } }, ctx.gtitle(g)),
    h("div.gm-banner.bad", icon("trash"), h("span", t("This deletes the game's files from the NAS. It can't be undone."))),
    h("div.gm-kvs",
      h("div.gm-kv", h("span", t("Folder on the NAS")), h("b.gm-mono", g.entry || g.path || "")),
      h("div.gm-kv", h("span", t("Size")), h("b", `${fmt.bytes(g.size)} · ${t("{n} files", { n: fmt.num(g.files || 0) })}`))),
    onDeck ? h("div.gm-note", t("It stays installed on the Steam Deck until you remove it there.")) : null,
    h("button.danger.lg", { onclick: (e) => act(async () => {
      await ctx.P("delete", { body: { id: g.id } });
      toast(t("Game deleted")); s.close(); onDone?.(); await ctx.refresh();
    }, e.currentTarget) }, icon("trash"), t("Delete {size}", { size: fmt.bytes(g.size) })),
    h("button", { onclick: () => s.close() }, t("Cancel")),
  ]);
}

// ---- remove a download: keep or delete its files ----------------------------------------------------------

export function removeDownload(ctx, d, onDone) {
  const active = d.state === "downloading" || d.state === "extracting";
  const info = h("div.gm-kvs", h("div.empty", h("span.spin")));
  const btns = h("div.stack");
  const done = async () => { s.close(); await onDone?.(); };
  const go = (files, btn) => act(async () => {
    await ctx.P(`find/downloads/${enc(d.hash)}${files ? "?files=1" : ""}`, { method: "DELETE" });
    toast(files ? t("Removed and files deleted") : t("Removed from the list")); await done();
  }, btn);
  const s = sheet(active ? t("Stop this download") : t("Remove download"), [
    h("div.gm-dtitle", { style: { fontSize: "17px" } }, d.title), info, btns]);
  const draw = (f) => {
    const p = f?.paths || [];
    if (active && d.state === "downloading") {
      info.replaceChildren(h("div.gm-note", t("The partial download is deleted with it.")));
      btns.replaceChildren(h("button.danger.lg", { onclick: (e) => go(false, e.currentTarget) }, icon("x"), t("Stop and remove")),
        h("button", { onclick: () => s.close() }, t("Keep downloading")));
      return;
    }
    info.replaceChildren(...(p.length ? [
      ...p.map((x) => h("div.gm-kv", h("span", t("Folder on the NAS")), h("b.gm-mono", x.path))),
      h("div.gm-kv", h("span", t("Size")), h("b", `${fmt.bytes(f.size)} · ${t("{n} files", { n: fmt.num(f.files) })}`)),
    ] : [h("div.gm-note", t("There are no files of this download left to delete."))]));
    btns.replaceChildren(...[
      h("button", { onclick: (e) => go(false, e.currentTarget) }, icon("x"), h("span.gm-btnt", t("Remove from list"), h("span.gm-sub2", t("The files stay on the NAS")))),
      p.length ? h("button.danger", { onclick: (e) => go(true, e.currentTarget) }, icon("trash"),
        h("span.gm-btnt", t("Remove and delete the files"), h("span.gm-sub2", fmt.bytes(f.size)))) : null,
      h("button.ghost", { onclick: () => s.close() }, t("Cancel"))].filter(Boolean));
  };
  if (d.state === "downloading") draw(null);
  else ctx.P(`find/downloads/${enc(d.hash)}/files`).then(draw, (e) => {
    info.replaceChildren(h("div.gm-note.err", e.message));
    btns.replaceChildren(h("button", { onclick: (ev) => go(false, ev.currentTarget) }, icon("x"), t("Remove from list")), h("button.ghost", { onclick: () => s.close() }, t("Cancel")));
  });
}
