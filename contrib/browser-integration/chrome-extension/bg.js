const HOST = "com.streamlink.redirect";

// Cloudflare Worker extractor. Preferred path — direct fetch, no native host needed.
// On failure the click falls back to the native host (which runs streamlink-redirect
// and forces the local vthreads-direct path via VTHREADS_SKIP_CLOUD=1).
const CLOUD_BASE = "https://extractor.bugcf.ccwu.cc";
const CLOUD_TOKEN = "test-token-2026-extractor";
const CLOUD_POLL_INTERVAL_MS = 2000;
const CLOUD_POLL_MAX_TRIES = 45;         // 45 × 2s = 90s ceiling before we bail to local
const CLOUD_SUBMIT_TIMEOUT_MS = 15000;   // submit POST hard-abort so cloud outages don't hang

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

// Mode: "cloud" (default, browser calls cloud extractor first) or "local"
// (skip cloud entirely, hand URL to native host which uses local vthreads).
// Persisted in chrome.storage.local, toggleable from the context menu.
const MODE_KEY = "sl_mode";
let currentMode = "cloud";

async function loadMode() {
  const stored = await chrome.storage.local.get(MODE_KEY);
  if (stored[MODE_KEY] === "cloud" || stored[MODE_KEY] === "local") {
    currentMode = stored[MODE_KEY];
  }
}

async function setMode(mode) {
  currentMode = mode;
  await chrome.storage.local.set({ [MODE_KEY]: mode });
  await rebuildMenus();
  notify("switched to " + mode + " mode", mode === "cloud" ? "Cloud" : "Local");
}

async function rebuildMenus() {
  await chrome.contextMenus.removeAll();
  const modeLabel = currentMode === "cloud" ? "☁ cloud" : "💻 local";
  chrome.contextMenus.create({
    id: "sl-quick",
    title: `Open in Streamlink (${modeLabel}, IINA, best)`,
    contexts: ["link", "page", "video", "selection"],
  });
  chrome.contextMenus.create({
    id: "sl-root",
    title: `Open in Streamlink... (${modeLabel})`,
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
  // Mode switch submenu — clearly labelled so you know which entry is active.
  chrome.contextMenus.create({
    id: "sl-sep",
    parentId: "sl-root",
    type: "separator",
    contexts: ["link", "page", "video", "selection"],
  });
  chrome.contextMenus.create({
    id: "sl-mode-cloud",
    parentId: "sl-root",
    type: "radio",
    checked: currentMode === "cloud",
    title: "Mode: ☁ Cloud first (fallback: local)",
    contexts: ["link", "page", "video", "selection"],
  });
  chrome.contextMenus.create({
    id: "sl-mode-local",
    parentId: "sl-root",
    type: "radio",
    checked: currentMode === "local",
    title: "Mode: 💻 Local only (skip cloud)",
    contexts: ["link", "page", "video", "selection"],
  });
}

chrome.runtime.onInstalled.addListener(async () => {
  await loadMode();
  await rebuildMenus();
});

chrome.runtime.onStartup.addListener(async () => {
  await loadMode();
  await rebuildMenus();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  // Mode switch clicks — no URL involved.
  if (info.menuItemId === "sl-mode-cloud") return setMode("cloud");
  if (info.menuItemId === "sl-mode-local") return setMode("local");

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

  console.log("[streamlink-redirect] clicked", { url, quality, player: player.name, mode: currentMode });

  // Local-only mode: hand straight to the native host, cloud never touched.
  if (currentMode === "local") {
    notify(player.name + " " + quality + " → resolving (local)", "Local");
    const payload = { url, quality, skip_cloud: true };
    if (player.scheme) payload.scheme = player.scheme;
    else if (player.app) payload.player = player.app;
    return sendToHost(payload, player, quality, "Local");
  }

  // Cloud-first mode: try cloud, fall back to local on any failure/timeout.
  notify(player.name + " " + quality + " → resolving (cloud)");
  const cloudResult = await tryCloudExtract(url, quality);
  if (cloudResult && cloudResult.direct_url) {
    notify(player.name + " " + quality + " → launching (cloud)");
    launchWithPlayer(player, cloudResult.direct_url);
    return;
  }

  // Cloud failed → local fallback (native host, skip_cloud so we don't loop).
  const payload = { url, quality, skip_cloud: true };
  if (player.scheme) payload.scheme = player.scheme;
  else if (player.app) payload.player = player.app;
  console.log("[streamlink-redirect] cloud failed, using native host", payload);
  notify("cloud unavailable, falling back to local", "Local");
  return sendToHost(payload, player, quality, "Local");
});

function sendToHost(payload, player, quality, subtitle) {
  chrome.runtime.sendNativeMessage(HOST, payload, (response) => {
    if (chrome.runtime.lastError) {
      const err = chrome.runtime.lastError.message;
      console.error("[streamlink-redirect] native error:", err);
      notify("host error: " + err, subtitle);
      return;
    }
    if (!response || !response.ok) {
      notify("failed: " + (response && response.error ? response.error : "unknown"), subtitle);
      return;
    }
    notify(player.name + " " + quality + " → launching (local, pid " + response.pid + ")", subtitle);
  });
}

async function tryCloudExtract(sourceUrl, quality) {
  const cloudQuality = canonCloudQuality(quality);
  const tag = `[cloud ${cloudQuality}]`;
  console.log(tag, "POST", `${CLOUD_BASE}/extract`, {source_url: sourceUrl, quality: cloudQuality});
  try {
    const submitCtrl = new AbortController();
    const submitTimer = setTimeout(() => submitCtrl.abort(), CLOUD_SUBMIT_TIMEOUT_MS);
    let submitRes;
    try {
      submitRes = await fetch(`${CLOUD_BASE}/extract`, {
        method: "POST",
        headers: { "X-Auth": CLOUD_TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify({ source_url: sourceUrl, quality: cloudQuality }),
        signal: submitCtrl.signal,
      });
    } finally {
      clearTimeout(submitTimer);
    }
    console.log(tag, "submit response status =", submitRes.status);
    const submitText = await submitRes.text();
    console.log(tag, "submit body =", submitText.slice(0, 500));
    if (!submitRes.ok) {
      console.warn(tag, "submit not ok");
      return null;
    }
    let submit;
    try { submit = JSON.parse(submitText); }
    catch (e) { console.warn(tag, "submit body not JSON:", e); return null; }
    if (submit.status === "success" && submit.direct_url) {
      console.log(tag, "cache-hit inline, direct_url =", submit.direct_url);
      return submit;
    }
    if (!submit.job_id) { console.warn(tag, "no job_id"); return null; }
    console.log(tag, "job_id =", submit.job_id, "polling every", CLOUD_POLL_INTERVAL_MS, "ms");

    for (let i = 0; i < CLOUD_POLL_MAX_TRIES; i++) {
      await sleep(CLOUD_POLL_INTERVAL_MS);
      const stRes = await fetch(`${CLOUD_BASE}/status/${submit.job_id}`, {
        headers: { "X-Auth": CLOUD_TOKEN },
      });
      if (!stRes.ok) { console.warn(tag, `status HTTP ${stRes.status} @try ${i}`); continue; }
      const st = await stRes.json();
      console.log(tag, `try ${i} status=${st.status} progress=${st.progress || 0}`);
      if (st.status === "success") {
        const rRes = await fetch(`${CLOUD_BASE}/result/${submit.job_id}`, {
          headers: { "X-Auth": CLOUD_TOKEN },
        });
        console.log(tag, "result response =", rRes.status);
        if (!rRes.ok) return null;
        const r = await rRes.json();
        console.log(tag, "final direct_url =", r.direct_url);
        return r;
      }
      if (st.status === "failed") {
        console.warn(tag, "job failed:", st.error);
        return null;
      }
    }
    console.warn(tag, "poll timed out after", CLOUD_POLL_MAX_TRIES, "tries");
    return null;
  } catch (err) {
    console.warn(tag, "fetch threw:", err.name, err.message, err.stack);
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

const NOTIFY_AUTO_CLEAR_MS = 4000;

function notify(message, subtitle) {
  // Default subtitle tracks the current mode so a Local-only session never
  // sees a "☁ Cloud" title.
  if (!subtitle) {
    subtitle = currentMode === "local" ? "Local" : "Cloud";
  }
  const glyph = subtitle === "Local" ? "💻" : "☁";
  chrome.notifications.create(
    {
      type: "basic",
      iconUrl: NOTIFY_ICON,
      title: "Streamlink " + glyph + " " + subtitle,
      message,
      // macOS honors this via Banner style; if the user has forced Alert style
      // in System Settings we can't override — the setTimeout below is a
      // belt-and-braces fallback that dismisses the notification ourselves.
      requireInteraction: false,
      silent: true,
    },
    (id) => {
      if (!id) return;
      setTimeout(() => chrome.notifications.clear(id), NOTIFY_AUTO_CLEAR_MS);
    },
  );
}
