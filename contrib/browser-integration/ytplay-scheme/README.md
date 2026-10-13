# ytplay:// URL scheme handler

Lets any web page (e.g. a local MyTube server) hand a video to the ytplay
enhance pipeline with one link - no extension, no nativeMessaging:

```
ytplay://play?url=<urlencoded>&quality=best&enhance=quality&player=iina
```

| param   | values                                              | default |
|---------|-----------------------------------------------------|---------|
| url     | http(s):// or file:// (urlencoded), required        | -       |
| quality | `best` or `<N>p` (2160p / 1440p / 1080p / ...)      | best    |
| enhance | `speed` \| `quality` \| `max` \| `photo` \| empty   | quality |
| player  | `senplayer` \| `iina`                               | iina    |

Unknown/invalid values fall back to the defaults (never passed through as
arbitrary CLI arguments - a web page can craft this URL).

## Install

```bash
./build_app.sh            # -> ~/Applications/YtplayScheme.app, registers ytplay://
```

Requires `streamlink-ytplay` on PATH (/opt/homebrew/bin and /usr/local/bin are
added automatically). Re-run after moving this directory (the handler script
path is baked into the applet).

## How it works

`YtplayScheme.app` is a tiny osacompile applet (LSUIElement, no Dock icon)
whose Info.plist registers the `ytplay` scheme. On open it runs
`ytplay_scheme_handler.py <url>`, which validates the parameters and spawns
`streamlink-ytplay <url> <quality> --enhance <mode> --player <player>
--idle-timeout 120` detached. ytplay serves the enhanced HLS locally and
launches the player, exactly like the browser-extension native host.

Logs: `/tmp/streamlink-scheme-handler.log` (handler),
`/tmp/streamlink-redirect.log` (ytplay child output, shared with the host).

## Telegram enhance-play (edge-play while downloading)

Telegram web videos have no public URL - the bytes are only fetchable inside the
logged-in web page (its service worker streams them from the DC, which is how
`telegram-enhance.user.js` reaches **public AND restricted** channels alike). So
instead of handing a URL to ytplay, the page downloads the media and pushes it in:

```
ytplay://ingest?enhance=photo&player=senplayer
```

- The scheme handler starts `streamlink-ytplay --ingest` on a fixed port (41599).
- `telegram-enhance.user.js` adds a ✨ button to the Telegram video controls. On
  click it **parallel-downloads** the video (8 Range lanes, reordered) at max
  speed and streams it to `http://127.0.0.1:41599/ingest`.
- ytplay writes a growing faststart mp4, **auto-detects the resolution** (ffprobe
  the moov - nothing hardcoded; picks the matching ANE model by source height),
  and starts enhancing from a ~2 MB buffer - **edge-play**: playback opens in
  SenPlayer while the download is still running. Because the parallel download
  outpaces 1x playback (and enhance runs >=1.4x realtime at 4K), it never stalls.
- Files whose moov is at the end (some file/document messages) can't edge-play;
  ytplay just starts once the whole file is received (small Telegram files + the
  parallel download make that fast).

Install the userscript in Tampermonkey; default tier is `photo` (edit `ENHANCE`
in the script, or the button's handler, to change it).

## Source URL notes

- Site pages (YouTube/PH/...): full yt-dlp extraction, streaming enhance.
- Direct HLS (.m3u8) URLs: streaming native enhance (best for servers).
- Direct progressive files (.mp4/...): downloaded whole to a local temp file,
  then enhanced (first frame waits for the download). Range requests are used
  when the server supports them; otherwise a sequential whole-body download.
- localhost / private-LAN / .local sources bypass the global proxy
  automatically.
- Dolby Vision P5/P7 direct files: auto-detected, BT.2020 reconstructed.
