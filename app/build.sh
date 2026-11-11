#!/bin/bash
# Build dist/Lineup.app (macOS 13+, needs Xcode Command Line Tools: `xcode-select --install`)
set -euo pipefail
cd "$(dirname "$0")/.."
APP="dist/Lineup.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -o "$APP/Contents/MacOS/Lineup" app/main.swift -framework AppKit
cp lineup.py "$APP/Contents/Resources/lineup.py"
cp assets/Lineup.icns "$APP/Contents/Resources/Lineup.icns"
VERSION=$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('lineup.py').read()).group(1))")
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Lineup</string>
  <key>CFBundleDisplayName</key><string>Lineup</string>
  <key>CFBundleIdentifier</key><string>app.lineup.Lineup</string>
  <key>CFBundleExecutable</key><string>Lineup</string>
  <key>CFBundleIconFile</key><string>Lineup</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true   # ad-hoc signature (local builds)
echo "built $APP"
