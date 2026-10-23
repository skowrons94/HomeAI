#!/usr/bin/env bash
# Install the Casa Deck agent on a Steam Deck (run it in Desktop Mode, in a terminal).
#
#   git clone https://github.com/skowrons94/HomeAI.git ~/casa && ~/casa/deploy/deck/install-deck.sh
#
# What it does (idempotent, safe to re-run; re-run it after updating the repo):
#   1. copies casa_deck_agent.py to ~/.local/bin
#   2. creates the shared token in ~/.config/casa-deck/token (kept on re-runs)
#   3. installs and starts a systemd *user* service "casa-deck" (no root, nothing outside $HOME)
# Then it prints the address and token to put in Casa → Settings → Games.
#
#   PORT=8765   agent port
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8765}"
BIN="$HOME/.local/bin"
CONF="$HOME/.config/casa-deck"
UNITS="$HOME/.config/systemd/user"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
mkdir -p "$BIN" "$CONF" "$UNITS"

say "Installing the agent"
install -m 755 "$SRC/casa_deck_agent.py" "$BIN/casa_deck_agent.py"

if [ ! -s "$CONF/token" ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$CONF/token")
fi
chmod 600 "$CONF/token"

cat > "$UNITS/casa-deck.service" <<UNIT
[Unit]
Description=Casa Deck agent (installs games sent by the Casa hub)
After=network-online.target

[Service]
ExecStart=/usr/bin/env python3 $BIN/casa_deck_agent.py --port $PORT --token-file $CONF/token
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  say "Starting the service"
  systemctl --user daemon-reload
  systemctl --user enable casa-deck.service
  systemctl --user restart casa-deck.service
  loginctl enable-linger "$USER" 2>/dev/null || echo "(could not enable linger: the agent runs while you are logged in, which includes Game Mode)"
else
  say "No systemd user session here: start it by hand with"
  echo "  python3 $BIN/casa_deck_agent.py --port $PORT --token-file $CONF/token"
fi

# SteamOS has no hostname command: fall back to ip (and never let this end the script)
IP="$( (hostname -I 2>/dev/null || ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') | awk '{print $1}' || true)"
say "Done. In Casa → Settings → Games enter:"
echo "  Steam Deck address : ${IP:-$(uname -n).local}   (or $(uname -n).local)"
echo "  Agent port         : $PORT"
echo "  Deck token         : $(cat "$CONF/token")"
echo
echo "Games go to ~/Games, ROMs to ~/Emulation/roms. Check: systemctl --user status casa-deck"
