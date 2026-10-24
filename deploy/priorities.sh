#!/usr/bin/env bash
# Keep the web app responsive while the Pi downloads, unpacks, scans or sends games:
#   - the hub (casa) gets more CPU/disk share and its memory is protected;
#   - Transmission and Navidrome yield (lower CPU weight, idle/low disk priority, soft memory cap);
#   - the network queue is fq_codel, so a big transfer can't delay the web app's small requests.
# Safe to re-run; installed by install-pi.sh.   Usage: sudo deploy/priorities.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

dropin() {   # dropin <unit> <file content>
  local unit="$1" body="$2"
  systemctl cat "$unit" >/dev/null 2>&1 || return 0          # not installed here
  mkdir -p "/etc/systemd/system/$unit.d"
  printf '%s\n' "$body" > "/etc/systemd/system/$unit.d/50-casa-priorities.conf"
  echo "  $unit"
}

echo "Setting service priorities"
dropin casa.service "[Service]
Nice=-5
CPUWeight=400
IOWeight=400
MemoryLow=120M"
dropin transmission-daemon.service "[Service]
Nice=10
CPUWeight=50
IOWeight=50
IOSchedulingClass=idle
MemoryHigh=200M"
dropin navidrome.service "[Service]
Nice=10
CPUWeight=50
IOWeight=50
IOSchedulingClass=best-effort
IOSchedulingPriority=7
MemoryHigh=250M"
systemctl daemon-reload

# fair queueing on the default route's interface, now and at every boot
IFACE="$(ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
if [ -n "$IFACE" ] && command -v tc >/dev/null; then
  cat > /etc/systemd/system/casa-fq.service <<UNIT
[Unit]
Description=Fair network queueing for Casa (fq_codel on $IFACE)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$(command -v tc) qdisc replace dev $IFACE root fq_codel
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now casa-fq.service >/dev/null 2>&1 && echo "  fq_codel on $IFACE"
fi

# apply to the running services (a restart of Transmission/Navidrome only pauses them for a moment)
for u in transmission-daemon navidrome; do
  systemctl is-active --quiet "$u" && systemctl restart "$u" || true
done
echo "Done. Restart the hub to apply its own priority: sudo systemctl restart casa"
