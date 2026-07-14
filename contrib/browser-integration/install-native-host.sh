#!/usr/bin/env bash
# Register the Streamlink Redirect native messaging host with Chrome / Chromium / Edge / Brave.
#
# The extension ID is baked in (derived from the checked-in public key in the
# extension's manifest.json), so this script takes no arguments — just run it once.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST_SCRIPT="$HERE/native-host/streamlink_redirect_host.py"
TEMPLATE="$HERE/native-host/com.streamlink.redirect.json.tpl"

if [[ ! -x "$HOST_SCRIPT" ]]; then
  chmod +x "$HOST_SCRIPT"
fi

MANIFEST_JSON=$(sed -e "s|__HOST_PATH__|$HOST_SCRIPT|" "$TEMPLATE")

install_for() {
  local dir="$1" label="$2"
  if ! mkdir -p "$dir" 2>/dev/null; then
    echo "skipped $label (no permission for $dir)"
    return
  fi
  if ! printf "%s\n" "$MANIFEST_JSON" > "$dir/com.streamlink.redirect.json" 2>/dev/null; then
    echo "skipped $label (write failed: $dir)"
    return
  fi
  echo "installed for $label: $dir/com.streamlink.redirect.json"
}

case "$(uname -s)" in
  Darwin)
    install_for "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" "Chrome"
    install_for "$HOME/Library/Application Support/Chromium/NativeMessagingHosts" "Chromium"
    install_for "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts" "Edge"
    install_for "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" "Brave"
    install_for "$HOME/Library/Application Support/Arc/User Data/NativeMessagingHosts" "Arc"
    install_for "$HOME/Library/Application Support/Vivaldi/NativeMessagingHosts" "Vivaldi"
    ;;
  Linux)
    install_for "$HOME/.config/google-chrome/NativeMessagingHosts" "Chrome"
    install_for "$HOME/.config/chromium/NativeMessagingHosts" "Chromium"
    install_for "$HOME/.config/microsoft-edge/NativeMessagingHosts" "Edge"
    install_for "$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts" "Brave"
    ;;
  *)
    echo "unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac

echo
echo "done. reload the extension in chrome://extensions if it was already open."
