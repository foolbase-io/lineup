#!/bin/bash
# Lineup installer.   ./install.sh [--yes] [--no-app] [--no-claude] [--login]
#   --yes        don't ask, accept defaults (connect Claude Code if found, build the app on macOS)
#   --no-app     skip building/installing Lineup.app (terminal view only)
#   --no-claude  don't touch Claude Code's settings
#   --login      open Lineup.app automatically when you log in (macOS)
set -euo pipefail
cd "$(dirname "$0")"

YES=0; APP=1; CLAUDE=1; LOGIN=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;; --no-app) APP=0 ;; --no-claude) CLAUDE=0 ;; --login) LOGIN=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done
ask() {  # ask "question" default(y/n)
  if [ "$YES" = 1 ]; then [ "$2" = y ]; return; fi
  local d="[Y/n]"; [ "$2" = n ] && d="[y/N]"
  read -r -p "$1 $d " r || r=""
  r="${r:-$2}"; [[ "$r" =~ ^[Yy] ]]
}

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
  echo "Lineup needs Python 3.8+ (macOS: xcode-select --install, or brew install python)."; exit 1
fi

LH="${LINEUP_HOME:-$HOME/.lineup}"
mkdir -p "$LH/agents"
cp lineup.py "$LH/lineup.py"
[ -f "$LH/config.json" ] || cp config.example.json "$LH/config.json"
echo "✓ engine installed in $LH"

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
cat > "$BIN/lineup" <<SH
#!/bin/sh
exec "$PY" "$LH/lineup.py" "\$@"
SH
chmod +x "$BIN/lineup"
echo "✓ command installed: $BIN/lineup"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  (add $BIN to your PATH to type 'lineup' anywhere)";; esac

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if [ "$CLAUDE" = 1 ] && { [ -d "$CLAUDE_DIR" ] || command -v claude >/dev/null 2>&1; }; then
  if ask "Connect Claude Code (adds hooks to $CLAUDE_DIR/settings.json, backup made first)?" y; then
    "$PY" "$LH/lineup.py" install claude-code | sed 's/^/✓ /'
  fi
fi
if [ -d "$HOME/.codex" ]; then echo "✓ Codex found - it's detected automatically, nothing to set up"; fi

if [ "$(uname)" = Darwin ] && [ "$APP" = 1 ]; then
  if command -v swiftc >/dev/null 2>&1; then
    ./app/build.sh >/dev/null
    mkdir -p "$HOME/Applications"
    rm -rf "$HOME/Applications/Lineup.app"
    cp -R dist/Lineup.app "$HOME/Applications/Lineup.app"
    echo "✓ app installed: ~/Applications/Lineup.app"
    if [ "$LOGIN" = 1 ] || { [ "$YES" = 0 ] && ask "Open Lineup automatically when you log in?" n; }; then
      PL="$HOME/Library/LaunchAgents/app.lineup.login.plist"
      mkdir -p "$(dirname "$PL")"
      cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>app.lineup.login</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>$HOME/Applications/Lineup.app</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
      [ -z "${LINEUP_NO_LAUNCHCTL:-}" ] && launchctl bootstrap "gui/$(id -u)" "$PL" 2>/dev/null || true
      echo "✓ opens at login"
    fi
  else
    echo "• skipped the app: no Swift compiler (xcode-select --install). The terminal view works: lineup"
  fi
fi

echo
echo "Done. Try:  lineup --demo        (or open ~/Applications/Lineup.app)"
echo "Connect anything else:  lineup report <agent> working \"what it's doing\"   (see README)"
