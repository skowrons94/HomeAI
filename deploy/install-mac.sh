#!/bin/zsh
# Run Casa in the background on a Mac (launchd agent: starts at login, restarts if it dies).
# Replaces the old standalone Ariston bridge agent if present.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.casa.hub
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OLD="$HOME/Library/LaunchAgents/com.ariston.homekit.plist"

[ -f "$OLD" ] && { launchctl unload "$OLD" 2>/dev/null || true; rm -f "$OLD"; echo "Removed old Ariston bridge agent."; }
pkill -f ariston_homekit.py 2>/dev/null || true

[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"
mkdir -p "$DIR/data"

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>-m</string><string>hub</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/casa.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/casa.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Casa running: http://localhost:8080   Logs: $DIR/data/casa.log"
echo "Stop/remove: launchctl unload $PLIST && rm $PLIST"
