#!/bin/bash
# Remove Lineup: hooks, app, login item, command, and (optionally) ~/.lineup.   ./uninstall.sh [--yes]
set -uo pipefail
YES=0; [ "${1:-}" = "--yes" ] && YES=1
LH="${LINEUP_HOME:-$HOME/.lineup}"
PY="$(command -v python3 || echo /usr/bin/python3)"
[ -f "$LH/lineup.py" ] && "$PY" "$LH/lineup.py" uninstall claude-code 2>/dev/null | sed 's/^/✓ /'
PL="$HOME/Library/LaunchAgents/app.lineup.login.plist"
if [ -f "$PL" ]; then
  [ -z "${LINEUP_NO_LAUNCHCTL:-}" ] && launchctl bootout "gui/$(id -u)/app.lineup.login" 2>/dev/null
  rm -f "$PL"; echo "✓ removed login item"
fi
pkill -f "Lineup.app/Contents/MacOS/Lineup" 2>/dev/null
rm -rf "$HOME/Applications/Lineup.app" && echo "✓ removed ~/Applications/Lineup.app"
rm -f "$HOME/.local/bin/lineup" && echo "✓ removed the lineup command"
if [ -d "$LH" ]; then
  if [ "$YES" = 1 ]; then r=y; else read -r -p "Delete $LH (config + agent state)? [y/N] " r || r=n; fi
  [[ "${r:-n}" =~ ^[Yy] ]] && rm -rf "$LH" && echo "✓ removed $LH"
fi
defaults delete app.lineup.Lineup >/dev/null 2>&1 || true
echo "Lineup removed. Thanks for surfing."
