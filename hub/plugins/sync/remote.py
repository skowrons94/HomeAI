"""Git side of the sync plugin: subprocess wrapper, the sync cycle (fetch, reset --hard, apply the outbox,
guard, commit, push) and the idempotent application of outbox operations. No rebase, no merge conflicts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path

from . import recipes_md
from .db import SyncDB
from .feed import append_events, scan_text, validate_event

log = logging.getLogger("hub.sync.remote")

SCHEMA_MD = """# Casa shared feed

Private repo shared by several Casa hubs. Only the social layer lives here.

- `feeds/<user>.jsonl`: append-only events (watch / rating / watchlist), written only by that user's hub
- `media/<imdb_id>.json|jpg`: title cards and posters, written once
- `recipes/<slug>.md|jpg`: shared recipes; the Trucchi section is append-only

Never put paths, hostnames, IPs, keys, torrents or library contents here.
"""
FOLDERS = ("feeds", "media", "recipes")


class GitError(RuntimeError):
    pass


class Remote:
    """Thin async wrapper around the git CLI for one working copy."""

    def __init__(self, url: str, branch: str, repo: Path, user: str, key: str | None = None, timeout: float = 90):
        self.url, self.branch, self.repo, self.user, self.key, self.timeout = url, branch, Path(repo), user, key, timeout

    def env(self) -> dict:
        e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": self.user,
             "GIT_AUTHOR_EMAIL": f"{self.user}@casa.invalid", "GIT_COMMITTER_NAME": self.user,
             "GIT_COMMITTER_EMAIL": f"{self.user}@casa.invalid"}
        if self.key:
            e["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(self.key)} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        return e

    async def git(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "commit.gpgsign=false", *args, cwd=self.repo, env=self.env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GitError(f"git {args[0]} timed out")
        if check and proc.returncode:
            raise GitError(f"git {args[0]} failed: {(err or out).decode(errors='replace').strip()[:300]}")
        return out.decode(errors="replace") if not proc.returncode else ""

    async def reset(self, _retry: bool = True) -> None:
        """Make the working copy equal to origin/<branch> (or empty if the remote is empty)."""
        if not (self.repo / ".git").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            await self.git("init", "-q", "-b", self.branch)
        if not await self.git("remote", check=False):
            await self.git("remote", "add", "origin", self.url)
        else:
            await self.git("remote", "set-url", "origin", self.url)
        await self.git("fetch", "-q", "--prune", "origin")
        ref = f"refs/remotes/origin/{self.branch}"
        if await self.git("rev-parse", "--verify", "-q", ref, check=False):
            await self.git("checkout", "-q", "-f", "-B", self.branch, ref)
            await self.git("reset", "-q", "--hard", ref)
            await self.git("clean", "-fdq")
        elif _retry:  # empty remote: start from scratch (nothing of value is local)
            shutil.rmtree(self.repo, ignore_errors=True)
            await self.reset(False)

    async def status(self) -> list[str]:
        out = await self.git("status", "--porcelain", "-uall")
        return [ln[3:].strip().strip('"') for ln in out.splitlines() if ln.strip()]

    async def commit_push(self, message: str) -> None:
        await self.git("add", "-A")
        await self.git("commit", "-q", "-m", message)
        await self.git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}")


class Syncer:
    """One sync cycle at a time (the plugin holds the lock)."""

    def __init__(self, db: SyncDB, remote: Remote, user: str, pending: Path, retries: int = 3):
        self.db, self.remote, self.user, self.pending, self.retries = db, remote, user, Path(pending), retries
        self.repo = remote.repo

    async def run(self) -> dict:
        for attempt in range(self.retries):
            await self.remote.reset()
            ops = self.db.outbox()
            counts = self.apply(ops)
            self.layout()
            await self.guard()
            if await self.remote.status():
                parts = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in counts.items() if n]
                try:
                    await self.remote.commit_push(f"{self.user}: {', '.join(parts) or 'update'}")
                except GitError as err:
                    log.info("push failed (attempt %d/%d): %s", attempt + 1, self.retries, err)
                    if attempt + 1 == self.retries:
                        raise
                    continue
            self.db.drop([o["seq"] for o in ops])
            self.cleanup(ops)
            self.db.rebuild(self.repo)
            return {"applied": len(ops), **counts}
        raise GitError("push rejected")

    # -- outbox ---------------------------------------------------------------------------------

    def apply(self, ops: list[dict]) -> dict:
        """Write every op into the working copy, idempotently. Returns what changed, by kind."""
        n = {"event": 0, "media": 0, "recipe": 0, "tip": 0}
        for o in ops:
            d = o["data"]
            try:
                n[o["op"]] += int(getattr(self, "_op_" + o["op"])(d))
            except (ValueError, OSError) as err:  # a bad op must not block the queue forever
                log.error("dropping outbox op %s: %s", o["op"], err)
        return n

    def _op_event(self, d: dict) -> bool:
        ev = validate_event(d["event"], self.user)
        return bool(append_events(self.repo / "feeds" / f"{self.user}.jsonl", [ev]))

    def _copy_once(self, src: str | None, dest: Path) -> bool:
        if not src or dest.exists() or not Path(src).is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return True

    def _op_media(self, d: dict) -> bool:
        imdb = d["imdb_id"]
        if not re.fullmatch(r"tt\d{5,10}", imdb):
            raise ValueError("bad imdb id")
        f = self.repo / "media" / f"{imdb}.json"
        changed = False
        if not f.exists():
            card = d["card"]
            scan_text(json.dumps(card, ensure_ascii=False), "a title card")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(card, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            changed = True
        return self._copy_once(d.get("poster_file"), self.repo / "media" / f"{imdb}.jpg") or changed

    def _read_recipe(self, slug: str) -> dict | None:
        f = self.repo / "recipes" / f"{slug}.md"
        return recipes_md.parse(f.read_text(encoding="utf-8")) if f.exists() else None

    def _write_recipe(self, slug: str, rec: dict) -> bool:
        f = self.repo / "recipes" / f"{slug}.md"
        text = recipes_md.render(rec)
        if f.exists() and f.read_text(encoding="utf-8") == text:
            return False
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return True

    def _op_recipe(self, d: dict) -> bool:
        slug = d["slug"]
        if not re.fullmatch(r"[a-z0-9-]+", slug):
            raise ValueError("bad slug")
        rec = recipes_md.validate_recipe(d["rec"])
        old = self._read_recipe(slug)
        if old and old["author"] != self.user:
            raise ValueError(f"recipe {slug} belongs to {old['author']}")
        if rec["author"] != self.user:
            raise ValueError("can only share your own recipes")
        mine = [t for t in rec["tips"] if t["user"] == self.user]
        rec["tips"] = recipes_md.merge_tips(old["tips"] if old else [], mine)
        changed = False
        if rec["image"]:
            changed = self._copy_once(d.get("image_file"), self.repo / "recipes" / rec["image"])
        return self._write_recipe(slug, rec) or changed

    def _op_tip(self, d: dict) -> bool:
        tip = recipes_md.check_tip(self.user, d["text"])
        old = self._read_recipe(d["slug"])
        if not old:
            raise ValueError(f"recipe {d['slug']} not found")
        old["tips"] = recipes_md.merge_tips(old["tips"], [tip])
        return self._write_recipe(d["slug"], old)

    def cleanup(self, ops: list[dict]) -> None:
        """Pushed posters/photos are served from the working copy now: drop their pending copies."""
        for o in ops:
            for k in ("poster_file", "image_file"):
                f = o["data"].get(k)
                if f and Path(f).parent == self.pending:
                    Path(f).unlink(missing_ok=True)

    def layout(self) -> None:
        """First hub on an empty repo: SCHEMA.md and the empty folders."""
        if not (self.repo / "SCHEMA.md").exists():
            (self.repo / "SCHEMA.md").write_text(SCHEMA_MD, encoding="utf-8")
        for f in FOLDERS:
            p = self.repo / f
            p.mkdir(exist_ok=True)
            if not any(p.iterdir()):
                (p / ".gitkeep").touch()

    # -- guard ----------------------------------------------------------------------------------

    async def guard(self) -> None:
        """Abort unless the working copy only differs by things this hub is allowed to write."""
        me = re.escape(self.user)
        ok = re.compile(rf"^(?:feeds/{me}\.jsonl|SCHEMA\.md|(?:feeds|media|recipes)/\.gitkeep"
                        rf"|media/tt\d{{5,10}}\.(?:json|jpg)|recipes/[a-z0-9-]+\.(?:md|jpg|png|webp))$")
        bad = [p for p in await self.remote.status() if not ok.match(p)]
        if bad:
            raise GitError(f"refusing to push unexpected changes: {', '.join(bad[:5])}")
        feed = self.repo / "feeds" / f"{self.user}.jsonl"
        if feed.exists():
            old = set((await self.remote.git("show", f"HEAD:feeds/{self.user}.jsonl", check=False)).splitlines())
            now = feed.read_text(encoding="utf-8").splitlines()
            if not old <= set(now):
                raise GitError("refusing to push: existing feed lines were changed")
            for line in now:
                if line not in old:
                    validate_event(json.loads(line), self.user)
        await self.remote.git("add", "-A")
        diff = await self.remote.git("diff", "--cached", "-U0", "--", "recipes", check=False)
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith(("+++", "+source: ")):
                try:
                    scan_text(line[1:], "a recipe line")
                except ValueError as err:
                    raise GitError(f"refusing to push: {err}")

