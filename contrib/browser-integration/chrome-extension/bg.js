const HOST = "com.streamlink.redirect";

// Cloudflare Worker extractor. Preferred path — direct fetch, no native host needed.
// On failure the click falls back to the native host (which runs streamlink-redirect
// and forces the local vthreads-direct path via VTHREADS_SKIP_CLOUD=1).
const CLOUD_BASE = "https://extractor.bugcf.ccwu.cc";
const CLOUD_TOKEN = "test-token-2026-extractor";
const CLOUD_POLL_INTERVAL_MS = 2000;
const CLOUD_POLL_MAX_TRIES = 90;

// URL scheme templates adapted from OpenList's player list. macOS-friendly players only.
// $edurl = percent-encoded resolved video URL. $durl = raw. See src/streamlink_cli/redirect.py.
const PLAYERS = [
  { id: "iina",      name: "IINA",       scheme: "iina://weblink?url=$edurl" },
  { id: "senplayer", name: "SenPlayer",  scheme: "senplayer://x-callback-url/play?url=$edurl" },
  { id: "vlc",       name: "VLC",        scheme: "vlc://$durl" },
  { id: "mpv",       name: "mpv",        scheme: "mpv://$edurl" },
  { id: "infuse",    name: "Infuse",     scheme: "infuse://x-callback-url/play?url=$durl" },
  { id: "omni",      name: "OmniPlayer", scheme: "omniplayer://weblink?url=$durl" },
  { id: "fig",       name: "Fig Player", scheme: "figplayer://weblink?url=$durl" },
  { id: "fileball",  name: "Fileball",   scheme: "filebox://play?url=$durl" },
  { id: "nplayer",   name: "nPlayer",    scheme: "nplayer-$durl" },
];

const QUALITIES = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p"];

const DEFAULT_PLAYER_ID = "iina";
const DEFAULT_QUALITY = "best";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "sl-quick",
    title: "Open in Streamlink (IINA, best)",
    contexts: ["link", "page", "video", "selection"],
  });
  chrome.contextMenus.create({
    id: "sl-root",
    title: "Open in Streamlink...",
    contexts: ["link", "page", "video", "selection"],
  });
  for (const p of PLAYERS) {
    chrome.contextMenus.create({
      id: `sl-p-${p.id}`,
      parentId: "sl-root",
      title: p.name,
      contexts: ["link", "page", "video", "selection"],
    });
    for (const q of QUALITIES) {
      chrome.contextMenus.create({
        id: `sl-p-${p.id}-q-${q}`,
        parentId: `sl-p-${p.id}`,
        title: q,
        contexts: ["link", "page", "video", "selection"],
      });
    }
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl || (tab && tab.url);
  if (!url) return notify("no URL to open");

  let playerId, quality;
  if (info.menuItemId === "sl-quick") {
    playerId = DEFAULT_PLAYER_ID;
    quality = DEFAULT_QUALITY;
  } else if (info.menuItemId.startsWith("sl-p-") && info.menuItemId.includes("-q-")) {
    const m = info.menuItemId.match(/^sl-p-(.+)-q-(.+)$/);
    playerId = m[1];
    quality = m[2];
  } else {
    return;
  }

  const player = PLAYERS.find((p) => p.id === playerId);
  if (!player) return notify("unknown player: " + playerId);

  console.log("[streamlink-redirect] clicked", { url, quality, player: player.name });
  notify(player.name + " " + quality + " → resolving (cloud)");

  // Path A: cloud extractor. Return early on success.
  const cloudResult = await tryCloudExtract(url, quality);
  if (cloudResult && cloudResult.direct_url) {
    notify(player.name + " " + quality + " → launching (cloud)");
    launchWithPlayer(player, cloudResult.direct_url);
    return;
  }

  // Path B: local streamlink-redirect via native host. Extension already tried cloud
  // and failed, so tell the plugin to skip cloud (VTHREADS_SKIP_CLOUD=1).
  const payload = { url, quality, skip_cloud: true };
  if (player.scheme) payload.scheme = player.scheme;
  else if (player.app) payload.player = player.app;

  console.log("[streamlink-redirect] cloud failed, using native host", payload);
  notify("cloud unavailable, falling back to local");
  chrome.runtime.sendNativeMessage(HOST, payload, (response) => {
    if (chrome.runtime.lastError) {
      const err = chrome.runtime.lastError.message;
      console.error("[streamlink-redirect] native error:", err);
      notify("host error: " + err);
      return;
    }
    if (!response || !response.ok) {
      notify("failed: " + (response && response.error ? response.error : "unknown"));
      return;
    }
    notify(player.name + " " + quality + " → launching (local, pid " + response.pid + ")");
  });
});

async function tryCloudExtract(sourceUrl, quality) {
  const cloudQuality = canonCloudQuality(quality);
  try {
    const submitRes = await fetch(`${CLOUD_BASE}/extract`, {
      method: "POST",
      headers: { "X-Auth": CLOUD_TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ source_url: sourceUrl, quality: cloudQuality }),
    });
    if (!submitRes.ok) {
      console.warn("[streamlink-redirect] cloud submit HTTP", submitRes.status);
      return null;
    }
    const submit = await submitRes.json();
    if (submit.status === "success" && submit.direct_url) return submit;
    if (!submit.job_id) return null;

    for (let i = 0; i < CLOUD_POLL_MAX_TRIES; i++) {
      await sleep(CLOUD_POLL_INTERVAL_MS);
      const stRes = await fetch(`${CLOUD_BASE}/status/${submit.job_id}`, {
        headers: { "X-Auth": CLOUD_TOKEN },
      });
      if (!stRes.ok) continue;
      const st = await stRes.json();
      if (st.status === "success") {
        const rRes = await fetch(`${CLOUD_BASE}/result/${submit.job_id}`, {
          headers: { "X-Auth": CLOUD_TOKEN },
        });
        if (!rRes.ok) return null;
        return await rRes.json();
      }
      if (st.status === "failed") {
        console.warn("[streamlink-redirect] cloud job failed:", st.error);
        return null;
      }
    }
    console.warn("[streamlink-redirect] cloud poll timed out");
    return null;
  } catch (err) {
    console.warn("[streamlink-redirect] cloud fetch threw:", err);
    return null;
  }
}

function launchWithPlayer(player, directUrl) {
  // Both scheme-based and app-based launches go through the native host with
  // `prefetched: true`. The host runs `open` on macOS, which correctly hands
  // the URL to the player without navigating away from the current browser tab
  // (chrome.tabs.update on an iina:// URL would blank the YouTube page).
  const payload = { url: directUrl, quality: "best", skip_cloud: true, prefetched: true };
  if (player.scheme) {
    payload.scheme = player.scheme;
  } else if (player.app) {
    payload.player = player.app;
  }
  chrome.runtime.sendNativeMessage(HOST, payload, (response) => {
    if (chrome.runtime.lastError) {
      console.error("[streamlink-redirect] launch host error:", chrome.runtime.lastError.message);
      notify("launch failed: " + chrome.runtime.lastError.message);
      return;
    }
    if (!response || !response.ok) {
      notify("launch failed: " + (response && response.error ? response.error : "unknown"));
    }
  });
}

function canonCloudQuality(q) {
  const s = String(q || "best").toLowerCase().trim();
  if (["best", "smallest", "audio_only"].includes(s)) return s;
  if (s === "worst") return "smallest";
  const aliases = { fhd: "1080p", qhd: "1440p", "2k": "1440p",
                    uhd: "2160p", "4k": "2160p", hd: "720p", sd: "480p" };
  if (aliases[s]) return aliases[s];
  const m = s.match(/(\d{3,4})/);
  return m ? `${m[1]}p` : "best";
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// 1x1 dark-gray PNG (chrome.notifications rejects SVG data-URLs — needs a real bitmap).
const NOTIFY_ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

function notify(message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: NOTIFY_ICON,
    title: "Streamlink Redirect",
    message,
  });
}
