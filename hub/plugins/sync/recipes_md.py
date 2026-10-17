"""Shared recipes as Markdown: tiny front matter + sections, slugs and the append-only Trucchi merge. Pure."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from .feed import NOTE_MAX, USER_RE, scan_text

SECTIONS = {"ingredienti": "ingredients", "ingredients": "ingredients",
            "procedimento": "steps", "preparazione": "steps", "steps": "steps", "method": "steps",
            "trucchi": "tips", "tips": "tips"}
IMAGE_RE = re.compile(r"^[a-z0-9-]+\.(?:jpg|png|webp)$")
TIP_RE = re.compile(r"^- \(([a-z0-9_-]{1,32})\) (.+)$")
_ONE = re.compile(r"\s+")


def one_line(s) -> str:
    return _ONE.sub(" ", str(s or "")).strip()


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60].strip("-") or "recipe"


def pick_slug(title: str, author: str, existing: dict[str, tuple[str, str]]) -> str:
    """``existing`` maps slug -> (title, author). Re-sharing your own recipe keeps its slug."""
    base = slugify(title)
    slug, n = base, 1
    while slug in existing and existing[slug] != (title, author):
        n += 1
        slug = f"{base}-{n}"
    return slug


def merge_tips(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union by (user, text): existing lines first and never dropped, then the new ones."""
    out, seen = [], set()
    for t in [*existing, *new]:
        k = (t["user"], t["text"])
        if k not in seen:
            seen.add(k)
            out.append({"user": t["user"], "text": t["text"]})
    return out


def check_source(url: str) -> str:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or " " in url:
        raise ValueError("source must be an http(s) link")
    if host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home")) \
            or re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        raise ValueError("source can't point to a local address")
    return url


def validate_recipe(rec: dict) -> dict:
    """Normalise a recipe dict and refuse anything private; returns a clean copy."""
    title = one_line(rec.get("title"))
    if not title or len(title) > 120:
        raise ValueError("a recipe needs a title (up to 120 characters)")
    scan_text(title, "the title")
    author = rec.get("author") or ""
    if not USER_RE.match(author):
        raise ValueError("bad author name")
    tags = []
    for t in rec.get("tags") or []:
        t = re.sub(r"[\[\],]", "", one_line(t)).lower()[:40]
        if t and t not in tags:
            tags.append(scan_text(t, "a tag"))
    num = {}
    for k in ("servings", "time_min"):
        v = rec.get(k)
        if v in (None, ""):
            num[k] = None
            continue
        v = float(v)
        if v <= 0 or v > 100000:
            raise ValueError(f"bad {k}")
        num[k] = int(v) if v == int(v) else round(v, 2)
    ing = [scan_text(one_line(x), "an ingredient") for x in rec.get("ingredients") or [] if one_line(x)]
    steps = [scan_text(one_line(x), "a step") for x in rec.get("steps") or [] if one_line(x)]
    tips = []
    for t in rec.get("tips") or []:
        tips.append(check_tip(t["user"], t["text"]))
    source = one_line(rec.get("source"))
    if source:
        check_source(source)
    image = one_line(rec.get("image"))
    if image and not IMAGE_RE.match(image):
        raise ValueError("bad image name")
    return {"title": title, "servings": num["servings"], "time_min": num["time_min"], "tags": tags,
            "author": author, "source": source or None, "image": image or None,
            "ingredients": ing, "steps": steps, "tips": merge_tips([], tips)}


def check_tip(user: str, text: str) -> dict:
    if not USER_RE.match(user or ""):
        raise ValueError("bad user name")
    text = one_line(text)
    if not text:
        raise ValueError("a tip can't be empty")
    if len(text) > NOTE_MAX:
        raise ValueError(f"a tip can be up to {NOTE_MAX} characters")
    scan_text(text, "a tip")
    return {"user": user, "text": text}


def render(rec: dict) -> str:
    """Write a (validated) recipe dict as Markdown."""
    fm = [f"title: {rec['title']}"]
    for k in ("servings", "time_min"):
        if rec.get(k) is not None:
            fm.append(f"{k}: {rec[k]}")
    fm.append(f"tags: [{', '.join(rec.get('tags') or [])}]")
    fm.append(f"author: {rec['author']}")
    for k in ("source", "image"):
        if rec.get(k):
            fm.append(f"{k}: {rec[k]}")
    steps, n = [], 0
    for s in rec.get("steps") or []:
        if s.startswith("# "):
            steps.append(s)
        else:
            n += 1
            steps.append(f"{n}. {s}")
    parts = ["---", *fm, "---", "## Ingredienti", *[f"- {x}" for x in rec.get("ingredients") or []], "",
             "## Procedimento", *steps, "", "## Trucchi",
             *[f"- ({t['user']}) {t['text']}" for t in rec.get("tips") or []]]
    return "\n".join(parts) + "\n"


def parse(text: str) -> dict:
    """Parse Markdown written by ``render`` (or by hand); raises ValueError if it isn't a recipe."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing front matter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("unterminated front matter")
    meta: dict = {}
    for ln in lines[1:end]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            meta[k.strip()] = v.strip()
    if not meta.get("title"):
        raise ValueError("recipe without title")

    def num(k):
        try:
            v = float(meta[k])
            return int(v) if v == int(v) else v
        except (KeyError, ValueError):
            return None

    tags_raw = meta.get("tags", "").strip()
    if tags_raw.startswith("[") and tags_raw.endswith("]"):
        tags_raw = tags_raw[1:-1]
    rec = {"title": meta["title"], "servings": num("servings"), "time_min": num("time_min"),
           "tags": [t.strip() for t in tags_raw.split(",") if t.strip()], "author": meta.get("author", ""),
           "source": meta.get("source") or None, "image": meta.get("image") or None,
           "ingredients": [], "steps": [], "tips": []}
    cur = None
    for ln in lines[end + 1:]:
        s = ln.strip()
        if s.startswith("## "):
            cur = SECTIONS.get(s[3:].strip().lower())
            continue
        if not s or cur is None:
            continue
        if cur == "ingredients":
            rec["ingredients"].append(s[2:].strip() if s.startswith("- ") else s)
        elif cur == "steps":
            rec["steps"].append(re.sub(r"^\d+[.)]\s+", "", s))
        else:
            m = TIP_RE.match(s)
            if m:
                rec["tips"].append({"user": m.group(1), "text": m.group(2).strip()})
    return rec
