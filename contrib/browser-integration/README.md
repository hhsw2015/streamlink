# Streamlink Redirect Browser Integration

Right-click a YouTube video in Chrome / Chromium / Edge / Brave / Arc / Vivaldi and hand the URL off to `streamlink-redirect`. A submenu lets you pick the player (IINA, SenPlayer, VLC, mpv, Infuse, …) and the quality (best / 2160p / 1440p / 1080p / …).

The extension talks to a **native messaging host** (spawned per click, exits immediately). The host launches `streamlink-redirect`, which:

1. Resolves the URL via Streamlink's `savenow` plugin (the plugin hits [video-download-api.com](https://video-download-api.com/) — `p.savenow.to` — and reuses / auto-provisions a free-start account with $1 credit).
2. Starts a local HTTP redirector on `127.0.0.1:8888` that 302s to the real mp4 URL.
3. Opens the player via its URL scheme (`iina://weblink?url=…`) or `open -a AppName …`.

**Every step is short-lived.** No daemon runs in the background.

## Files

```
contrib/browser-integration/
├── install-native-host.sh              # registers the host with Chrome-family browsers
├── native-host/
│   ├── streamlink_redirect_host.py     # host (stdin/stdout JSON)
│   └── com.streamlink.redirect.json.tpl# host manifest template
├── chrome-extension/
│   ├── manifest.json                   # MV3 extension; extension ID is baked in via the `key` field
│   └── bg.js                           # right-click menu + native messaging
├── extractor-worker/                   # optional cloud path: CF Worker snapshot
└── README.md
```

## Install

Extension ID is fixed: `goipkfhlkdneflgagfhedbndnpdcebch` (derived from the public key committed in `chrome-extension/manifest.json`). No IDs to copy.

1. **Register the native host** (once, no arguments):
   ```bash
   cd contrib/browser-integration
   ./install-native-host.sh
   ```
   Installs manifests under `~/Library/Application Support/<Browser>/NativeMessagingHosts/` for Chrome / Chromium / Edge / Brave / Arc / Vivaldi (macOS) or `~/.config/<browser>/NativeMessagingHosts/` (Linux). Directories that aren't writable are skipped with a message.

2. **Load the extension** in your browser:
   - Open `chrome://extensions` (or `arc://extensions` in Arc, etc.)
   - Enable **Developer mode**
   - Click **Load unpacked** → pick `contrib/browser-integration/chrome-extension/`
   - Verify the extension ID matches `goipkfhlkdneflgagfhedbndnpdcebch`

3. **Verify `streamlink-redirect` is on PATH** for GUI apps:
   ```bash
   which streamlink-redirect
   # e.g. /opt/homebrew/bin/streamlink-redirect
   ```
   The host script prepends `/opt/homebrew/bin` and `/usr/local/bin` to PATH already, so a Homebrew symlink is enough.

Done.

## Use

Right-click on a YouTube page or link. Two menu entries:

- **Open in Streamlink (IINA, best)** — one-click, uses IINA at `best` quality.
- **Open in Streamlink…** → *Player* → *Quality* — nested submenu for full control.

Players: IINA, SenPlayer, VLC, mpv, Infuse, OmniPlayer, Fig Player, Fileball, nPlayer.
Qualities: `best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`.

macOS notifications keep you posted: "resolving 1080p …", "1080p ready, launching player", or "failed: <error>".

## Supported sites

**YouTube only** (watch / shorts / embed / youtu.be).

Earlier iterations proxied through vthreads.top and supported 20+ sites; vthreads paywalled itself in mid-2026, so the plugin was rewritten against the savenow.to API. Live tests show TikTok / Bilibili / Twitter / Instagram / Facebook / Vimeo / Reddit / Pornhub all come back as `Failed` from savenow; only YouTube (+ Dailymotion) actually resolves. The matcher is scoped accordingly.

## Cloud extractor (optional)

The extension can be flipped to "☁ Cloud first" mode via the context-menu submenu. In that mode it POSTs to a Cloudflare Worker (default `https://extractor.bugcf.ccwu.cc`) which fronts savenow.to with a shared account pool. Falling back to "💻 Local only" bypasses the Worker entirely and runs the local plugin.

Source snapshot for the Worker lives in [`extractor-worker/`](./extractor-worker/) — see its README for redeploying.

## Account pool

The savenow plugin persists a per-user account pool in `~/.cache/streamlink-savenow/accounts.json`. When every stored key runs out of credit, `SavenowAccountPool.register_new()` auto-signs up a fresh account (no email verification) which comes with $1 of credit ≈ 5 000 downloads. Env knobs:

- `SAVENOW_API_KEY` — pin a specific key, skips the pool entirely.
- `SAVENOW_AUTOREGISTER=0` — disable auto-signup on exhaustion (fail instead).
- `SAVENOW_ENDPOINT` — override the default `p.savenow.to` host (useful when the dashboard assigns you a custom hostname).

Cost estimation (see `_savenow_accounts.py:estimated_cost`) mirrors the [pricing table](https://video-download-api.com/pricing): base fee per format, 30 min base duration for 4K/8K, 3× / 5× / +2 multipliers past the base. Before submitting a request the plugin picks a key that has enough balance for the worst case, otherwise it rotates.

## Redirect server lifecycle

`streamlink-redirect --once --idle-timeout 120`:

- Startup with no connection → exits after 120 s.
- Player connects → active connection keeps the server up.
- Player disconnects → 120 s of "no player" grace period.
- Another player connects during the grace period → timer resets.

Per-click server, not shared. Multiple simultaneous videos = multiple servers on different ports (falls back to 8889 / 8890 / … if 8888 is busy).

## Logs

| Symptom | Where to look |
|---|---|
| Right-click menu missing / clicks silent | `chrome://extensions` → your extension → **service worker** → DevTools **Console** |
| Menu click sends nothing / host errors | `tail -f /tmp/streamlink-native-host.log` |
| Host started but playback fails | `tail -f /tmp/streamlink-redirect.log` |

Tail both host logs at once:

```bash
tail -f /tmp/streamlink-*.log
```

## Uninstall

- Remove the extension from `chrome://extensions`.
- Delete host manifests:
  ```bash
  rm ~/Library/Application\ Support/Google/Chrome/NativeMessagingHosts/com.streamlink.redirect.json
  # ...and the same file under any of Chromium / Arc / Vivaldi
  ```
- (Optional) Clear the cache / burn all registered accounts:
  ```bash
  rm -rf ~/.cache/streamlink-savenow
  ```

## Notes

- **No daemon**. Chrome spawns the host per message and reaps it after the reply. The `streamlink-redirect` child runs detached (`start_new_session=True`) and exits on idle timeout.
- **Port**: fixed at 8888, auto-fallback to 8889 / 8890 / … if busy.
- **Bind host**: default `127.0.0.1`. Add `--host 0.0.0.0` on the redirect command line to expose on the LAN (phones / TVs).
- **Not published to the Chrome Web Store.** Load unpacked only.
