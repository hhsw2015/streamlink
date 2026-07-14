const HOST = "com.streamlink.redirect";

// URL scheme templates adapted from OpenList's player list. Only entries usable on macOS.
// $edurl = percent-encoded resolved video URL. $durl = raw. See src/streamlink_cli/redirect.py.
// Two entry types: `scheme` = URL-scheme handoff (preferred, works cross-browser).
// `app` = `open -a AppName URL` (macOS only, but works for players without a documented scheme).
const PLAYERS = [
  { id: "iina",      name: "IINA",       scheme: "iina://weblink?url=$edurl" },
  { id: "senplayer", name: "SenPlayer",  app: "SenPlayer" },
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
  // Fast-path top-level items — default player, common qualities
  chrome.contextMenus.create({
    id: "sl-quick",
    title: "Open in Streamlink (IINA, best)",
    contexts: ["link", "page", "video", "selection"],
  });

  // Nested submenu for full control
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

chrome.contextMenus.onClicked.addListener((info, tab) => {
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
    return; // parent-only click, ignore
  }

  const player = PLAYERS.find((p) => p.id === playerId);
  if (!player) return notify("unknown player: " + playerId);

  const payload = { url, quality };
  if (player.scheme) payload.scheme = player.scheme;
  else if (player.app) payload.player = player.app;
  console.log("[streamlink-redirect] sending", { ...payload, playerLabel: player.name });
  chrome.runtime.sendNativeMessage(
    HOST,
    payload,
    (response) => {
      if (chrome.runtime.lastError) {
        const err = chrome.runtime.lastError.message;
        console.error("[streamlink-redirect] native error:", err);
        notify("host error: " + err);
        return;
      }
      console.log("[streamlink-redirect] response:", response);
      if (!response || !response.ok) {
        notify("failed: " + (response && response.error ? response.error : "unknown"));
        return;
      }
      notify(player.name + " " + quality + " → resolving (pid " + response.pid + ")");
    },
  );
});

function notify(message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'><rect width='64' height='64' fill='%23222'/><text x='50%25' y='55%25' font-size='40' text-anchor='middle' fill='white' font-family='sans-serif'>SL</text></svg>",
    title: "Streamlink Redirect",
    message,
  });
}
