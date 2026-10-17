"""Translations shared by the web app and the hub.

``web/i18n/<lang>/*.json`` map English source text to translated text, one file per area
(``core.json``, ``media.json`` ...) so they can be edited independently. The web app gets the
merged map from ``GET /api/i18n``; the hub translates its own messages (notifications,
attention items) with ``_()``. A missing entry falls back to the English text.

    _("Ready to watch: {what}", what=title)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("hub.i18n")

ROOT = Path(__file__).resolve().parent.parent / "web" / "i18n"
LANGUAGES = {"it": "Italiano", "en": "English"}
DEFAULT = "it"

_lang = DEFAULT
_texts: dict[str, str] = {}


def load(lang: str) -> dict[str, str]:
    """The merged map for ``lang`` (empty for English, the source language)."""
    out: dict[str, str] = {}
    for f in sorted((ROOT / lang).glob("*.json")) if lang != "en" else []:
        try:
            out.update(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            log.warning("bad translation file %s: %s", f, err)
    return out


def set_language(lang: str | None) -> None:
    global _lang, _texts
    _lang = lang if lang in LANGUAGES else DEFAULT
    _texts = load(_lang)


def language() -> str:
    return _lang


def texts() -> dict[str, str]:
    return _texts


def _(text: str, **kw) -> str:
    s = _texts.get(text, text)
    if not kw:
        return s
    try:
        return s.format(**kw)
    except (KeyError, IndexError, ValueError):  # a broken translation: show the English one
        return text.format(**kw)
