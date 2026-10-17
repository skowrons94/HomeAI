"""Minimal async Transmission RPC client (transmission-daemon 3.x / 4.x)."""

from __future__ import annotations

import base64

import aiohttp

FIELDS = ["id", "hashString", "name", "status", "percentDone", "rateDownload", "rateUpload",
          "eta", "totalSize", "sizeWhenDone", "leftUntilDone", "downloadDir", "error",
          "errorString", "uploadRatio", "isFinished", "doneDate", "addedDate", "peersConnected",
          "metadataPercentComplete"]
STATUS = {0: "stopped", 1: "check_wait", 2: "checking", 3: "download_wait", 4: "downloading",
          5: "seed_wait", 6: "seeding"}


class TransmissionError(Exception):
    pass


class Transmission:
    def __init__(self, cfg: dict):
        self.url = cfg.get("url", "http://127.0.0.1:9091/transmission/rpc")
        user, pw = cfg.get("username"), cfg.get("password")
        self.auth = aiohttp.BasicAuth(user, pw or "") if user else None
        self.session_id = ""
        self.http: aiohttp.ClientSession | None = None

    async def open(self):
        # local daemon: never through the indexer proxy
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self):
        if self.http:
            await self.http.close()

    async def call(self, method: str, **arguments) -> dict:
        body = {"method": method, "arguments": arguments}
        for _ in range(2):
            try:
                async with self.http.post(self.url, json=body, auth=self.auth,
                                          headers={"X-Transmission-Session-Id": self.session_id}) as r:
                    if r.status == 409:  # CSRF dance: retry with the new session id
                        self.session_id = r.headers.get("X-Transmission-Session-Id", "")
                        continue
                    if r.status == 401:
                        raise TransmissionError("Transmission: wrong username/password")
                    r.raise_for_status()
                    data = await r.json(content_type=None)
            except aiohttp.ClientConnectorError as err:
                raise TransmissionError(f"Transmission not reachable at {self.url}") from err
            if data.get("result") != "success":
                raise TransmissionError(f"Transmission: {data.get('result')}")
            return data.get("arguments", {})
        raise TransmissionError("Transmission: session negotiation failed")

    async def add(self, magnet: str = "", download_dir: str | None = None, paused=False,
                  torrent: bytes | None = None) -> dict:
        """Add a magnet link, or the raw bytes of a .torrent file."""
        args = {"paused": paused}
        if torrent:
            args["metainfo"] = base64.b64encode(torrent).decode()
        else:
            args["filename"] = magnet
        if download_dir:
            args["download-dir"] = download_dir
        res = await self.call("torrent-add", **args)
        t = res.get("torrent-added") or res.get("torrent-duplicate") or {}
        return {"id": t.get("id"), "hash": (t.get("hashString") or "").lower(), "name": t.get("name")}

    async def torrents(self, hashes: list[str] | None = None, files=False) -> list[dict]:
        args = {"fields": FIELDS + (["files", "wanted"] if files else [])}
        if hashes is not None:
            if not hashes:
                return []
            args["ids"] = hashes
        res = await self.call("torrent-get", **args)
        out = []
        for t in res.get("torrents", []):
            t["hash"] = t["hashString"].lower()
            t["state"] = STATUS.get(t["status"], "unknown")
            out.append(t)
        return out

    async def only_file(self, hash_: str, file_idx: int, count: int) -> None:
        """Download a single file of a pack (episode out of a season)."""
        unwanted = [i for i in range(count) if i != file_idx]
        if unwanted:
            await self.call("torrent-set", ids=[hash_], **{"files-unwanted": unwanted,
                                                          "files-wanted": [file_idx]})

    async def remove(self, hash_: str, delete_data=False) -> None:
        await self.call("torrent-remove", ids=[hash_], **{"delete-local-data": delete_data})

    async def stop(self, hash_: str) -> None:
        await self.call("torrent-stop", ids=[hash_])

    async def start(self, hash_: str) -> None:
        await self.call("torrent-start", ids=[hash_])

    async def session(self) -> dict:
        return await self.call("session-get")

    async def set_download_dir(self, path: str) -> bool:
        """Point Transmission's default download folder at ``path``; True if it changed."""
        cur = (await self.session()).get("download-dir")
        if cur == path:
            return False
        await self.call("session-set", **{"download-dir": path})
        return True
