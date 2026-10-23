#!/usr/bin/env bash
# Install Casa on a Raspberry Pi (Raspberry Pi OS Bookworm / Debian 12+, 64-bit).
#
#   git clone / copy this folder to the Pi, e.g. ~/casa, then:
#   ./deploy/install-pi.sh
#
# What it does (idempotent, safe to re-run):
#   1. apt: python venv, BlueZ, Transmission, cifs-utils, smbclient
#   2. Python venv + requirements
#   3. NAS: root mount helper (lets the app mount/unmount NAS shares) and, unless
#      NAS_FSTAB=0, a boot-time /etc/fstab mount of the default share at /mnt/nas
#      (NAS_FSTAB=0 if the NAS is already mounted, e.g. by your own fstab lines;
#      MOUNT=... and DOWNLOAD_DIR=... point the installer at those folders)
#   4. Transmission: RPC on localhost only, downloads on the NAS. Skipped (and an
#      existing daemon disabled) with TRANSMISSION=0 or when config.toml has
#      [plugins.media.transmission] enabled = false
#   5. systemd service "casa" (starts at boot, restarts on failure)
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
NAS_HOST="${NAS_HOST:-NAS.local}"
NAS_SHARE="${NAS_SHARE:-Multimedia}"
MOUNT="${MOUNT:-/mnt/nas}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-$MOUNT/downloads/incomplete}"   # where Transmission writes
# Transmission on this machine? Default: what config.toml says (missing/true = yes).
if [ -z "${TRANSMISSION:-}" ]; then
  TRANSMISSION="$(python3 - "$DIR/config.toml" <<'PY' 2>/dev/null || echo 1
import sys, tomllib
try:
    c = tomllib.load(open(sys.argv[1], "rb"))
except FileNotFoundError:
    c = {}
tx = c.get("plugins", {}).get("media", {}).get("transmission", {})
print(0 if tx.get("enabled", True) is False else 1)
PY
)"
fi
if [ "$TRANSMISSION" = 1 ]; then TX_GROUP=debian-transmission; else TX_GROUP="$(id -gn)"; fi

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

say "Installing system packages"
sudo apt-get update -qq
PKGS="python3-venv python3-dev bluez cifs-utils smbclient libffi-dev libssl-dev"
[ "$TRANSMISSION" = 1 ] && PKGS="$PKGS transmission-daemon"
# shellcheck disable=SC2086
sudo apt-get install -y -qq $PKGS

say "Creating Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip wheel
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
# Bluetooth (Govee) through BlueZ/D-Bus needs the bluetooth group
sudo usermod -aG bluetooth "$USER_NAME"

say "NAS mount helper (casa-mount + sudoers rule)"
sudo install -m 755 -o root -g root "$DIR/deploy/casa-mount" /usr/local/sbin/casa-mount
SUDOERS_TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/casa-mount" > "$SUDOERS_TMP"
if sudo visudo -cf "$SUDOERS_TMP" >/dev/null; then
  sudo install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/casa-mount
else
  echo "!! sudoers rule invalid, not installed"
fi
rm -f "$SUDOERS_TMP"

say "NAS mount ($NAS_HOST/$NAS_SHARE -> $MOUNT)"
# NAS_FSTAB=0 skips this: configure the NAS in the app (Settings > Storage & NAS) instead.
if [ "${NAS_FSTAB:-1}" = 1 ]; then
CRED=/etc/casa-nas.cred
if [ ! -f "$CRED" ]; then
  read -rp "NAS username (empty = guest): " NAS_USER
  if [ -n "$NAS_USER" ]; then
    read -rsp "NAS password: " NAS_PASS; echo
    printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PASS" | sudo tee "$CRED" >/dev/null
  else
    printf 'username=guest\npassword=\n' | sudo tee "$CRED" >/dev/null
  fi
  sudo chmod 600 "$CRED"
fi
sudo mkdir -p "$MOUNT"
# Transmission runs as debian-transmission: give that group write access too (if installed).
# vers=2.0 because the TS-419P II firmware is old; raise to 3.0 if your NAS allows it.
FSTAB="//$NAS_HOST/$NAS_SHARE $MOUNT cifs credentials=$CRED,uid=$USER_NAME,gid=$TX_GROUP,file_mode=0775,dir_mode=0775,vers=2.0,iocharset=utf8,_netdev,x-systemd.automount,nofail 0 0"
if ! grep -q " $MOUNT cifs" /etc/fstab; then
  echo "$FSTAB" | sudo tee -a /etc/fstab >/dev/null
fi
sudo systemctl daemon-reload
sudo mount "$MOUNT" 2>/dev/null || true
ls "$MOUNT" >/dev/null && echo "NAS mounted: $(ls "$MOUNT" | tr '\n' ' ')" \
  || echo "!! NAS not mounted yet - check $CRED and the share name, then: sudo mount $MOUNT"
[ "$TRANSMISSION" = 1 ] && sudo usermod -aG debian-transmission "$USER_NAME"
mkdir -p "$DOWNLOAD_DIR" 2>/dev/null || true
mkdir -p "$MOUNT/games/pc" "$MOUNT/games/emu" 2>/dev/null || true   # games plugin library (docs/games.md)
else
  [ "$TRANSMISSION" = 1 ] && sudo usermod -aG debian-transmission "$USER_NAME"
  sudo mkdir -p "$MOUNT"
fi

if [ "$TRANSMISSION" = 1 ]; then
say "Configuring Transmission"
sudo systemctl stop transmission-daemon
SETTINGS=/etc/transmission-daemon/settings.json
sudo python3 - "$SETTINGS" "$DOWNLOAD_DIR" <<'PY'
import json, sys
path, dl = sys.argv[1], sys.argv[2]
s = json.load(open(path))
s.update({
    "download-dir": dl,
    "incomplete-dir-enabled": False,
    "rpc-enabled": True,
    "rpc-bind-address": "127.0.0.1",      # only Casa talks to it
    "rpc-authentication-required": False,
    "rpc-whitelist-enabled": True,
    "rpc-whitelist": "127.0.0.1",
    "umask": 2,                           # group-writable, so Casa can move files
    "ratio-limit-enabled": True,
    "ratio-limit": 1.0,
    "speed-limit-up-enabled": False,
    "cache-size-mb": 16,                  # keep RAM use low on the Pi
    "peer-limit-global": 120,
})
json.dump(s, open(path, "w"), indent=4)
PY
sudo systemctl enable --now transmission-daemon
else
  say "Transmission disabled on this machine"
  if systemctl cat transmission-daemon.service >/dev/null 2>&1; then
    sudo systemctl disable --now transmission-daemon 2>/dev/null || true
    echo "transmission-daemon stopped and disabled (it won't start at boot)."
  fi
fi

say "Configuration"
if [ ! -f "$DIR/config.toml" ]; then
  cp "$DIR/config.example.toml" "$DIR/config.toml"
  chmod 600 "$DIR/config.toml"
  if [ "$TRANSMISSION" != 1 ]; then  # keep the hub away from Transmission as well
    sed -i 's/^# enabled = false\( *# never contact Transmission\)/enabled = false\1/' "$DIR/config.toml"
  fi
  echo "Created config.toml - edit it (Ariston login, TMDB key, ...) then: sudo systemctl restart casa"
fi

say "Installing systemd service"
sudo tee /etc/systemd/system/casa.service >/dev/null <<UNIT
[Unit]
Description=Casa home hub
After=network-online.target bluetooth.target transmission-daemon.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m hub
Restart=on-failure
RestartSec=5
# be gentle with the SD card and other services
Nice=5
MemoryMax=400M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now casa

IP="$(hostname -I | awk '{print $1}')"
say "Done"
echo "Web UI:     http://$IP:8080   (or http://$(hostname).local:8080)"
echo "Logs:       journalctl -u casa -f"
echo "HomeKit:    setup code + QR in the web UI under Settings"
echo "VPN (opt.): ./deploy/vpn-wireguard.sh /path/to/provider-wg.conf"
