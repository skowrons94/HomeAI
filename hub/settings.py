"""config.toml, editable from the app.

The file stays the single source of truth and readable by hand; the Settings
page edits it through ``Config`` and the hub applies the change live (plugins
restart with the new values) or asks for a restart (web port, HomeKit).

Forms are generated from schemas: ``CORE`` below for the hub itself and
``Plugin.schema`` for each plugin (field format documented in CLAUDE.md).
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

SECRET = "••••••••"  # stands in for a stored password; sent back unchanged = keep it

CORE: dict[str, dict] = {
    "general": {
        "title": "General", "table": None, "restart": False,
        "fields": [
            {"key": "name", "label": "Home name", "type": "text", "default": "Casa"},
            {"key": "rooms", "label": "Rooms", "type": "list", "default": [],
             "help": "Used to group devices on the Home page."},
        ],
    },
    "notify": {
        "title": "Notifications", "table": "notify", "restart": False,
        "fields": [
            {"key": "ntfy_url", "label": "ntfy topic URL", "type": "text",
             "placeholder": "https://ntfy.sh/casa-something-secret",
             "help": "Install the free ntfy app, subscribe to a hard-to-guess topic, paste its URL."},
            {"key": "telegram_token", "label": "Telegram bot token", "type": "password",
             "advanced": True},
            {"key": "telegram_chat_id", "label": "Telegram chat id", "type": "text",
             "advanced": True},
        ],
    },
    "history": {
        "title": "History", "table": "history", "restart": False,
        "fields": [
            {"key": "detail_days", "label": "Keep 1-minute detail for", "type": "number",
             "default": 2, "min": 1, "max": 30, "unit": "days"},
            {"key": "medium_days", "label": "Keep 5-minute averages for", "type": "number",
             "default": 30, "min": 7, "max": 365, "unit": "days"},
            {"key": "long_days", "label": "Keep hourly averages for", "type": "number",
             "default": 730, "min": 30, "max": 3650, "unit": "days"},
        ],
    },
    "web": {
        "title": "Web app", "table": "web", "restart": True,
        "fields": [
            {"key": "port", "label": "Port", "type": "number", "default": 8080},
            {"key": "password", "label": "Password", "type": "password",
             "help": "Leave empty to open the app without a password (fine on a home network)."},
        ],
    },
    "homekit": {
        "title": "Apple Home", "table": "homekit", "restart": True,
        "fields": [
            {"key": "enabled", "label": "HomeKit bridge", "type": "bool", "default": True},
            {"key": "name", "label": "Bridge name", "type": "text", "default": "Casa"},
            {"key": "port", "label": "Port", "type": "number", "default": 51826,
             "advanced": True},
        ],
    },
}


class Config:
    def __init__(self, path: Path):
        self.path = path
        with path.open("rb") as f:
            self.data: dict = tomllib.load(f)

    def save(self) -> None:
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_bytes(tomli_w.dumps(_clean(self.data)).encode())
        tmp.chmod(0o600)
        if self.path.exists():
            bak = self.path.with_suffix(".toml.bak")
            bak.write_bytes(self.path.read_bytes())
            bak.chmod(0o600)
        os.replace(tmp, self.path)

    # -- core sections ------------------------------------------------------------

    def section(self, name: str) -> dict:
        table = CORE[name]["table"]
        return self.data if table is None else self.data.setdefault(table, {})

    def core_values(self, name: str) -> dict:
        return values_for(CORE[name]["fields"], self.section(name))

    def set_core(self, name: str, values: dict) -> None:
        apply_values(CORE[name]["fields"], self.section(name), values)
        self.save()

    # -- plugins ----------------------------------------------------------------

    def plugin(self, name: str) -> dict | None:
        cfg = (self.data.get("plugins") or {}).get(name)
        return cfg if isinstance(cfg, dict) else None

    def plugin_enabled(self, name: str) -> bool:
        cfg = self.plugin(name)
        return cfg is not None and cfg.get("enabled", True) is not False

    def set_plugin(self, name: str, schema: list, values: dict | None,
                   enabled: bool | None = None) -> dict:
        cfg = self.data.setdefault("plugins", {}).setdefault(name, {})
        if values:
            apply_values(schema, cfg, values)
        if enabled is not None:
            if enabled:
                cfg.pop("enabled", None)
            else:
                cfg["enabled"] = False
        self.save()
        return cfg

    def update_plugin(self, name: str, **values) -> None:
        """For plugins storing results (e.g. a pairing id) in their own config."""
        cfg = self.data.setdefault("plugins", {}).setdefault(name, {})
        cfg.update(values)
        self.save()


# -- schema helpers ------------------------------------------------------------------


def get_path(d: dict, key: str, default: Any = None) -> Any:
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return default
        d = d[part]
    return d


def set_path(d: dict, key: str, value: Any) -> None:
    *parents, last = key.split(".")
    for part in parents:
        d = d.setdefault(part, {})
    if value is None:
        d.pop(last, None)
    else:
        d[last] = value


def values_for(schema: list, cfg: dict) -> dict:
    out = {}
    for f in schema:
        v = get_path(cfg, f["key"], f.get("default"))
        if f.get("type") == "password":
            v = SECRET if v else ""
        out[f["key"]] = v
    return out


def apply_values(schema: list, cfg: dict, values: dict) -> None:
    for f in schema:
        if f["key"] not in values:
            continue
        v = values[f["key"]]
        if f.get("type") == "password" and v == SECRET:
            continue  # unchanged
        set_path(cfg, f["key"], coerce(f, v))


def coerce(field: dict, v: Any) -> Any:
    """Form value -> TOML value; None removes the key (the default applies)."""
    t = field.get("type", "text")
    if t == "bool":
        return bool(v)
    if t == "list":
        if isinstance(v, str):
            v = [x.strip() for x in v.replace("\n", ",").split(",")]
        return [x for x in (v or []) if x not in ("", None)]
    if v is None or v == "":
        return None
    if t == "number":
        n = float(v)
        for bound, cmp in (("min", max), ("max", min)):
            if field.get(bound) is not None:
                n = cmp(n, field[bound])
        return int(n) if n == int(n) and not isinstance(field.get("step"), float) else n
    if t == "select" and field.get("options"):
        allowed = [o["value"] if isinstance(o, dict) else o for o in field["options"]]
        if v not in allowed:
            raise ValueError(f"{field['label']}: {v!r} is not one of {allowed}")
    return str(v).strip()


def _clean(d: Any) -> Any:
    """Drop None values (TOML has no null) and copy."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(v) for v in d if v is not None]
    return copy.copy(d)
