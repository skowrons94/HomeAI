#!/usr/bin/env bash
# Route the Pi's internet traffic through a WireGuard VPN (Mullvad, Proton VPN,
# AirVPN, IVPN, ... - download a WireGuard config file from your provider).
#
#   ./deploy/vpn-wireguard.sh ~/Downloads/provider-wg.conf
#   ./deploy/vpn-wireguard.sh --off          # disable again
#
# * The home LAN (HomeKit, NAS, speakers, TV) keeps working: only internet
#   traffic goes through the tunnel.
# * DNS goes through the VPN too, so trackers blocked by the ISP's DNS
#   become reachable for the media search.
# * Kill-switch: Transmission is bound to the VPN address and stops when the
#   tunnel goes down, so torrent traffic never leaks outside the VPN.
#
# Want only the tracker searches through a proxy instead? Set
# [plugins.media.proxy] url = "socks5://..." in config.toml - no VPN needed.
set -euo pipefail

DROPIN=/etc/systemd/system/transmission-daemon.service.d/vpn.conf
SETTINGS=/etc/transmission-daemon/settings.json

set_bind() {  # $1 = IPv4 address Transmission binds to
  sudo systemctl stop transmission-daemon
  sudo python3 - "$SETTINGS" "$1" <<'PY'
import json, sys
s = json.load(open(sys.argv[1])); s["bind-address-ipv4"] = sys.argv[2]
json.dump(s, open(sys.argv[1], "w"), indent=4)
PY
}

if [ "${1:-}" = "--off" ]; then
  sudo systemctl disable --now wg-quick@wg0 || true
  sudo rm -f "$DROPIN"
  set_bind "0.0.0.0"
  sudo systemctl daemon-reload
  sudo systemctl start transmission-daemon
  echo "VPN disabled; Transmission uses the normal connection."
  exit 0
fi

CONF="${1:?usage: $0 provider-wg.conf | --off}"
[ -f "$CONF" ] || { echo "No such file: $CONF"; exit 1; }

sudo apt-get install -y -qq wireguard-tools openresolv
sudo install -m 600 "$CONF" /etc/wireguard/wg0.conf

# The tunnel address (first IPv4 in "Address = 10.x.x.x/32, fc00::/128")
VPN_IP="$(sed -n 's/^Address *= *//p' /etc/wireguard/wg0.conf | tr ',' '\n' | grep -m1 -oE '([0-9]+\.){3}[0-9]+')"
[ -n "$VPN_IP" ] || { echo "Couldn't find an IPv4 Address in the config"; exit 1; }

sudo systemctl enable --now wg-quick@wg0
sleep 3
echo "Public IP through VPN: $(curl -s -m 10 https://api.ipify.org || echo '?')"

set_bind "$VPN_IP"
sudo mkdir -p "$(dirname "$DROPIN")"
sudo tee "$DROPIN" >/dev/null <<'UNIT'
[Unit]
# kill-switch: Transmission only runs while the tunnel is up
BindsTo=wg-quick@wg0.service
After=wg-quick@wg0.service
UNIT
sudo systemctl daemon-reload
sudo systemctl start transmission-daemon
echo "VPN on. Transmission bound to $VPN_IP. Check: sudo wg show"
