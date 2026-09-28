// Pre-extract trigger. On right-click (contextmenu) we GUESS the URL the menu is
// about to act on and ask the background to prewarm it - run extraction +
// bootstrap ahead of time - so choosing "Play" a moment later skips the ~4s
// extraction wait. Best-effort and debounced: a wrong guess just wastes one warm
// session (which self-expires), it never breaks playback.
//
// The URL is resolved with the SAME priority the context-menu handler uses
// (link -> media src -> page), so the prewarm key matches what Play will request.
(function () {
  let lastUrl = "";
  let lastAt = 0;

  function targetUrl(e) {
    const a = e.target && e.target.closest && e.target.closest("a[href]");
    if (a && a.href) return a.href;                       // info.linkUrl
    const t = e.target;
    if (t && (t.tagName === "VIDEO" || t.tagName === "AUDIO" || t.tagName === "IMG")) {
      if (t.currentSrc || t.src) return t.currentSrc || t.src;   // info.srcUrl
    }
    const src = t && t.querySelector && t.querySelector("source[src]");
    if (src && src.src) return src.src;
    return location.href;                                  // info.pageUrl
  }

  document.addEventListener("contextmenu", (e) => {
    let url;
    try { url = targetUrl(e); } catch (_) { return; }
    if (!url || !/^https?:\/\//.test(url)) return;
    const now = Date.now();
    if (url === lastUrl && now - lastAt < 5000) return;   // debounce repeat right-clicks
    lastUrl = url; lastAt = now;
    try { chrome.runtime.sendMessage({ type: "slPrewarm", url }); } catch (_) { /* worker asleep */ }
  }, true);

  // Page-load / SPA-navigation prewarm for recognized WATCH pages. Extraction (the
  // ~4-20s network floor) runs WHILE the user is still watching in-browser, so a
  // later "Play in player" opens near-instantly. The fan stays off: the prewarm
  // lazy-boots (no GPU until the player connects); the ~0.7s fast-tier seg0 GPU is
  // paid only at the click. Only known video sites' watch pages fire here (right-
  // click covers everything else), dwell-gated so a page you just click through
  // never extracts. The native host + bg de-dupe, so an over-eager fire is cheap.
  const WATCH = [
    /^https?:\/\/(www\.)?youtube\.com\/watch\?[^#]*\bv=/,
    /^https?:\/\/youtu\.be\/[\w-]+/,
    /^https?:\/\/(www\.)?bilibili\.com\/video\//,
    /^https?:\/\/vimeo\.com\/\d+/,
  ];
  const DWELL_MS = 1200;      // filter fast click-throughs; still starts extraction early
  let dwellTimer = 0;

  function watchUrl() {
    const u = location.href;
    return WATCH.some((re) => re.test(u)) ? u : null;
  }

  function schedulePagePrewarm() {
    clearTimeout(dwellTimer);
    const url = watchUrl();
    if (!url) return;
    dwellTimer = setTimeout(() => {
      const now = Date.now();
      if (url !== location.href) return;                    // navigated away during dwell
      if (url === lastUrl && now - lastAt < 30000) return;  // already warmed this URL recently
      lastUrl = url; lastAt = now;
      try { chrome.runtime.sendMessage({ type: "slPrewarm", url }); } catch (_) { /* worker asleep */ }
    }, DWELL_MS);
  }

  // Initial load + SPA navigations (YouTube/Bilibili swap videos without a full
  // reload). pushState is used inconsistently across sites, so poll location.href.
  schedulePagePrewarm();
  let lastHref = location.href;
  setInterval(() => {
    if (location.href !== lastHref) { lastHref = location.href; schedulePagePrewarm(); }
  }, 1000);
})();
