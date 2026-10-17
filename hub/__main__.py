"""Run the hub:  python -m hub [--config config.toml] [--debug]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .core import Hub
from .settings import Config
from .web import WebServer

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> Config:
    if not path.exists():
        # a fresh install starts empty: the web app's Welcome wizard sets it up
        path.write_text('name = "Casa"\n\n[web]\nport = 8080\n\n[homekit]\nname = "Casa"\n')
        path.chmod(0o600)
    return Config(path)


async def run(cfg: Config) -> bool:
    """Run until stopped; True if a restart was requested from the app."""
    hub = Hub(cfg, ROOT)
    await hub.start()
    web = WebServer(hub, cfg.data.get("web", {}), ROOT / "web")
    await web.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, hub.shutdown.set)
    await hub.shutdown.wait()

    logging.getLogger("hub").info("restarting" if hub.restart_requested else "shutting down")
    await web.stop()
    await hub.stop()
    return hub.restart_requested


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

    if asyncio.run(run(load_config(args.config))):
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "hub", *sys.argv[1:]])


if __name__ == "__main__":
    main()
