"""Run the hub:  python -m hub [--config config.toml] [--debug]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import tomllib
from pathlib import Path

from .core import Hub
from .web import WebServer

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Missing {path}. Copy config.example.toml to config.toml and edit it.")
    with path.open("rb") as f:
        return tomllib.load(f)


async def run(cfg: dict) -> None:
    hub = Hub(cfg, ROOT)
    await hub.start()
    web = WebServer(hub, cfg.get("web", {}), ROOT / "web")
    await web.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    logging.getLogger("hub").info("shutting down")
    await web.stop()
    await hub.stop()


def main() -> None:
    ap = argparse.ArgumentParser(prog="hub")
    ap.add_argument("--config", type=Path, default=ROOT / "config.toml")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # chatty libraries
    for name in ("pyhap.hap_protocol", "pyhap.hap_handler", "aiohttp.access", "bleak"):
        logging.getLogger(name).setLevel(logging.WARNING)

    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    main()
