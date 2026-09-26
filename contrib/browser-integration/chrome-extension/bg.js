const HOST = "com.streamlink.redirect";

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

// Friendly names for the enhance tiers, shared by the menu and the toasts.
const ENH_NAME = { off: "关闭", speed: "速度", quality: "质量", max: "极限" };

async function rebuildMenus() {
  await chrome.contextMenus.removeAll();
  // ytplay: the primary path. yt-dlp pipeline — any site, max quality, full seek.
  // Top-level entries are one-click "best"; each player also gets a quality submenu.
  const YTPLAY_QUALITIES = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p"];
  chrome.contextMenus.create({
    id: "sl-yt-senplayer-q-best",
    title: "▶ Play in SenPlayer (best)",
    contexts: ["link", "page", "video", "selection"],
  });
  chrome.contextMenus.create({
    id: "sl-yt-iina-q-best",
    title: "▶ Play in IINA (best)",
    contexts: ["link", "page", "video", "selection"],
  });
  for (const pid of ["senplayer", "iina"]) {
    const parent = `sl-yt-${pid}-more`;
    chrome.contextMenus.create({
      id: parent,
      title: (pid === "senplayer" ? "SenPlayer" : "IINA") + " quality...",
      contexts: ["link", "page", "video", "selection"],
    });
    for (const q of YTPLAY_QUALITIES) {
      chrome.contextMenus.create({
        id: `sl-yt-${pid}-sub-${q}`,
        parentId: parent,
        title: q,
        contexts: ["link", "page", "video", "selection"],
      });
    }
  }
  // Enhance selector: local GPU/ANE AI upscale before the stream reaches the
  // player. Any yt-dlp site. Three tiers on Apple Silicon (measured):
  //   speed   = MetalFX, guaranteed 4K60, smoothest (no CNN, no temporal).
  //   quality = CuNNy CNN super-res + motion-compensated temporal reconstruction.
  //   max     = ArtCNN on the Neural Engine (heaviest, most detail) + the same
  //             temporal reconstruction.
  // BOTH CNN tiers denoise/recover compressed sources (dense GPU optical flow +
  // multi-frame merge, +2-3dB luma): they differ in the SR model, not the
  // temporal stage. Clean sources run 4K60; temporal (compressed) settles ~4K30.
  const { enhanceMode } = await chrome.storage.local.get({ enhanceMode: "off" });
  chrome.contextMenus.create({
    id: "sl-enhance-menu",
    title: "✨ 画质增强",
    contexts: ["link", "page", "video", "selection"],
  });
  const ENH = [
    ["off", "关闭"],
    ["speed", "速度 · MetalFX · 稳 4K60,最流畅"],
    ["quality", "质量 · CuNNy 超分 + 时域重建"],
    ["max", "极限 · ArtCNN 超分(神经引擎)+ 时域重建,细节最强"],
  ];
  for (const [mode, label] of ENH) {
    chrome.contextMenus.create({
      id: `sl-enhance-${mode}`,
      parentId: "sl-enhance-menu",
      title: label,
      type: "radio",
      checked: enhanceMode === mode,
      contexts: ["link", "page", "video", "selection"],
    });
  }
  // Legacy tree (streamlink-redirect + cloud extractor + savenow) removed:
  // savenow stopped granting free credit, so that chain dead-ends at
  // "Balance insufficient". ytplay (yt-dlp pipeline) is the only path now.
  // Other players remain reachable via ytplay's PLAYER_SCHEMES if re-added.
}

chrome.runtime.onInstalled.addListener(async () => {
  await rebuildMenus();
});

chrome.runtime.onStartup.addListener(async () => {
  await rebuildMenus();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  // Enhance mode: persists in storage, applied to every subsequent play.
  const enhMatch = String(info.menuItemId).match(/^sl-enhance-(.+)$/);
  if (enhMatch && enhMatch[1] !== "menu") {
    const mode = enhMatch[1];
    await chrome.storage.local.set({ enhanceMode: mode });
    notify("画质增强: " + (mode === "off" ? "已关闭" : (ENH_NAME[mode] || mode) + " 已开启 ✨"), "Local");
    return;
  }

  const url = info.linkUrl || info.srcUrl || info.pageUrl || (tab && tab.url);
  if (!url) return notify("no URL to open");

  // ytplay engine: yt-dlp pipeline via native host, site cookies attached.
  // IDs: sl-yt-<player>-q-best (top level) and sl-yt-<player>-sub-<quality>.
  const ytMatch = String(info.menuItemId).match(/^sl-yt-(senplayer|iina)-(?:q|sub)-(.+)$/);
  if (ytMatch) {
    const playerId = ytMatch[1];
    const quality = ytMatch[2];
    const player = PLAYERS.find((p) => p.id === playerId);
    const { enhanceMode } = await chrome.storage.local.get({ enhanceMode: "off" });
    const enhance = enhanceMode !== "off" ? enhanceMode : "";
    notify(player.name + " " + quality + (enhance ? " ✨" + (ENH_NAME[enhance] || enhance) : "") + " → resolving (ytplay)", "Local");
    const cookies = await collectCookies(url);
    const payload = {
      engine: "ytplay",
      url,
      quality,
      scheme: playerId,   // ytplay knows senplayer/iina by name
      cookies,
      enhance,            // "" | "speed" | "quality" | "max"
    };
    return sendToHost(payload, player, quality, "Local");
  }

  // Anything else: legacy menu ids from an older service worker instance.
  console.warn("[streamlink-redirect] unknown menu id:", info.menuItemId);
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

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Collect cookies for the video page's site (login-walled sites: Vimeo,
// members-only videos, age gates). Sent to the native host, which writes a
// Netscape cookies.txt for yt-dlp. Only the target site's cookies — never
// the whole jar.
async function collectCookies(pageUrl) {
  try {
    const host = new URL(pageUrl).hostname;
    // Base domain heuristic: keep last two labels (good enough for the
    // yt-dlp site list; ccTLD registrable-domain edge cases just send fewer
    // cookies, which only means the site behaves as if logged out).
    const parts = host.split(".");
    const base = parts.slice(-2).join(".");
    const cookies = await chrome.cookies.getAll({ domain: base });
    return cookies.map((c) => ({
      domain: c.domain,
      path: c.path,
      secure: c.secure,
      expirationDate: c.expirationDate,
      name: c.name,
      value: c.value,
    }));
  } catch (e) {
    console.warn("[streamlink-redirect] cookie collect failed:", e);
    return [];
  }
}

// 1x1 dark-gray PNG (chrome.notifications rejects SVG data-URLs — needs a real bitmap).
const NOTIFY_ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

const NOTIFY_AUTO_CLEAR_MS = 4000;

function notify(message, subtitle) {
  // Default subtitle tracks the current mode so a Local-only session never
  // sees a "☁ Cloud" title.
  if (!subtitle) subtitle = "Local";
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
