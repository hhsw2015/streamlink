#!/bin/bash
# Build + register YtplayScheme.app (ytplay:// URL scheme -> streamlink-ytplay).
# Usage: ./build_app.sh [install-dir]   (default: ~/Applications)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/YtplayScheme.app"

mkdir -p "$DEST"
rm -rf "$APP"

# AppleScript applet: receives the URL via an "open location" handler and
# hands it to the python handler script (absolute path baked in).
osacompile -o "$APP" <<EOF
on open location theURL
    do shell script "/usr/bin/python3 " & quoted form of "$HERE/ytplay_scheme_handler.py" & " " & quoted form of theURL & " >/dev/null 2>&1 &"
end open location
EOF

# Register the ytplay:// scheme in the applet's Info.plist.
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes array' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0 dict' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0:CFBundleURLName string ytplay' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes array' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string ytplay' "$PLIST"
/usr/libexec/PlistBuddy -c 'Delete :CFBundleIdentifier' "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string com.streamlink.ytplay-scheme' "$PLIST"
# Background-only: no Dock icon flash on every click.
/usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' "$PLIST"

# Re-sign (plist changed) + register with LaunchServices.
codesign --force --sign - "$APP" 2>/dev/null || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
echo "installed: $APP (scheme ytplay:// registered)"
