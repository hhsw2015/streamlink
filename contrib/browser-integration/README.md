# Streamlink Redirect Browser Integration

Right-click any video page (YouTube, TikTok, Bilibili, ...) in Chrome / Chromium / Edge / Brave / Arc / Vivaldi and hand the URL off to `streamlink-redirect`. A submenu lets you pick the player (IINA, SenPlayer, VLC, mpv, Infuse, ...) and the quality (best / 2160p / 1440p / 1080p / ...).

The extension talks to a **native messaging host** (spawned per click, exits immediately). The host launches `streamlink-redirect`, which:

1. Resolves the URL via Streamlink's `vthreads` plugin (server-side yt-dlp + optional merge).
2. Starts a local HTTP redirector on `127.0.0.1:8888` that 302s to the real mp4 URL.
3. Opens the player via its URL scheme (`iina://weblink?url=...`) or `open -a AppName ...`.

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
└── README.md
```

## Install

Extension ID is fixed: `goipkfhlkdneflgagfhedbndnpdcebch` (derived from the public key committed in `chrome-extension/manifest.json`). No IDs to copy.

1. **Register the native host** (once, no arguments):
   ```bash
   cd contrib/browser-integration
   ./install-native-host.sh
   ```
   Installs manifests under `~/Library/Application Support/<Browser>/NativeMessagingHosts/` for Chrome, Chromium, Edge, Brave, Arc, Vivaldi (macOS) or `~/.config/<browser>/NativeMessagingHosts/` (Linux). Browsers whose directory is not writable are skipped with a message.

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

Right-click on:
- a video page (YouTube, TikTok, Bilibili, ...)
- any link to a supported site
- a `<video>` element

Two menu entries:

- **Open in Streamlink (IINA, best)** — one-click, uses IINA at `best` quality.
- **Open in Streamlink...** → *Player* → *Quality* — nested submenu for full control.

Players in the submenu: IINA, SenPlayer, VLC, mpv, Infuse, OmniPlayer, Fig Player, Fileball, nPlayer.

Qualities: `best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`.

macOS notifications keep you posted: "resolving 1080p ...", "1080p ready, launching player", or "failed: <error>".

## Supported sites

The `vthreads` plugin (`src/streamlink/plugins/vthreads.py`) resolves any URL matching its whitelist. Currently:

**Global**: YouTube (+ Shorts), Twitter/X, Instagram, Facebook, Reddit, TikTok, Vimeo, Dailymotion, Twitch (VOD/clips), SoundCloud, Rumble, Odysee, BitChute, Streamable, TED, Archive.org
**中区**: Bilibili, 微博, 快手, 小红书, 抖音, v.qq, 爱奇艺, 芒果, 优酷, 西瓜
**Adult**: Pornhub, Xvideos, xHamster, RedTube, YouPorn, SpankBang
**Other**: ok.ru

Add a host by editing the matcher regex in `vthreads.py`.

## How it avoids upstream rate limits

- **Whitelist matcher** — the plugin does not touch vthreads unless the URL is a known video host.
- **Cross-process cache** — same URL clicked twice in a row reuses the resolved mp4 without calling the API. Cache files live under `~/.cache/streamlink-vthreads/` and expire after 5 min (extract) / 10 min (merge URL).
- **Chrome UA + Referer** — every request looks like a normal browser session.
- **429 / 5xx backoff** — retries with 3-5 s pauses (up to 5 attempts) before giving up.
- **Poll loop tolerates transient errors** — 5 consecutive poll failures required before aborting.

## Redirect server lifecycle

`streamlink-redirect --once --idle-timeout 120`:

- Startup with no connection → exits after 120 s.
- Player connects → active connection keeps the server up indefinitely.
- Player disconnects → 120 s of "no player" grace period.
- Another player connects during the grace period → timer resets.

The server is per-click, not shared. Multiple simultaneous videos = multiple servers on different ports (redirect falls back to 8889, 8890, ... if 8888 is busy).

## Logs

Three places, three purposes:

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
- (Optional) Clear the cache:
  ```bash
  rm -rf ~/.cache/streamlink-vthreads
  ```

## Notes

- **No daemon**. Chrome spawns the host per message and reaps it after the reply. The `streamlink-redirect` child runs detached (`start_new_session=True`) and exits on idle timeout.
- **Port**: fixed at 8888, auto-fallback to 8889/8890/... if busy.
- **Bind host**: default `127.0.0.1`. Add `--host 0.0.0.0` to the host script command line to expose on the LAN (for phones / TVs).
- **Not published to the Chrome Web Store.** Load unpacked only.
