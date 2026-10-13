// ==UserScript==
// @name         Telegram Enhance Play (ytplay)
// @namespace    https://github.com/streamlink/ytplay
// @version      1.0
// @description  Add a "✨ Enhance Play" button to Telegram web videos. Parallel-downloads the media (public OR restricted, via the service worker) at max speed and pushes it to the local ytplay native enhance pipeline (4K AI upscale -> IINA/mpv), edge-playing while it downloads.
// @match        https://web.telegram.org/*
// @match        https://webk.telegram.org/*
// @match        https://webz.telegram.org/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  "use strict";

  // --- config -------------------------------------------------------------
  const INGEST_PORT = 41599;          // must match the ytplay:// scheme handler
  const INGEST = `http://127.0.0.1:${INGEST_PORT}`;
  const ENHANCE = "photo";            // default tier (speed|quality|max|photo)
  const PLAYER = "senplayer";         // senplayer | iina
  // Max-speed download: many parallel Range connections. Download MUST outpace 1x
  // playback or it stutters, so we saturate the link. Telegram's service worker
  // serves each Range from the DC; 8 lanes * 4 MB is a good saturation point.
  const LANES = 16;                  // concurrent range connections (max speed)
  const CHUNK = 4 << 20;             // 4 MB push window (assembled from ~512KB
                                     // sub-reads; Telegram caps each range response)

  const log = (...a) => console.log("[tg-enhance]", ...a);

  // --- on-page status toast (so there's always visible feedback) -----------
  let toastEl = null;
  const toast = (msg, ms) => {
    if (!toastEl) {
      toastEl = document.createElement("div");
      Object.assign(toastEl.style, {
        position: "fixed", bottom: "24px", left: "50%", transform: "translateX(-50%)",
        background: "rgba(0,0,0,.85)", color: "#fff", padding: "10px 16px",
        borderRadius: "10px", font: "14px -apple-system,sans-serif", zIndex: 99999,
        pointerEvents: "none", maxWidth: "80vw", textAlign: "center",
      });
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = "✨ " + msg;
    toastEl.style.display = "block";
    if (toast._t) clearTimeout(toast._t);
    if (ms) toast._t = setTimeout(() => { toastEl.style.display = "none"; }, ms);
  };

  // --- fire a custom scheme via an anchor click (an iframe is blocked by
  // Telegram's CSP frame-src; location.href can interrupt the page). MUST be
  // called inside the user gesture (external-protocol launch is gesture-gated). --
  const fireScheme = (uri) => {
    const a = document.createElement("a");
    a.href = uri;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
  };

  // --- wait for the ingest server (ytplay --ingest) to come up -------------
  const serverReady = async (timeoutMs = 8000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        // Any HTTP status (200/404) proves the server answered; a thrown error
        // means it's not up yet. GET avoids the PNA ambiguity of a bare OPTIONS.
        await fetch(`${INGEST}/enh/index.m3u8`, { method: "GET" });
        return true;
      } catch (e) { /* connection refused -> not up yet */ }
      await new Promise((r) => setTimeout(r, 200));
    }
    return false;
  };

  // --- total size via a 1-byte Range probe --------------------------------
  const probeSize = async (url) => {
    const r = await fetch(url, { method: "GET", headers: { Range: "bytes=0-0" } });
    if (![200, 206].includes(r.status)) throw new Error("probe status " + r.status);
    const cr = r.headers.get("Content-Range");
    await r.arrayBuffer();
    const m = cr && cr.match(/\/(\d+)\s*$/);
    if (m) return { size: parseInt(m[1]), ranged: true };
    // No Range support: fall back to a single whole-file fetch.
    const cl = r.headers.get("Content-Length");
    return { size: cl ? parseInt(cl) : 0, ranged: false };
  };

  // --- parallel, in-order download -> single streaming POST to /ingest ----
  let busy = false;   // debounce: a second click must not respawn/kill an active run
  const enhancePlay = async () => {
    if (busy) { log("already starting; ignoring repeat click"); return; }
    busy = true;
    try {
      await _enhancePlay();
    } finally {
      // keep busy for a moment after so the scheme's kill+respawn can't race a
      // rapid second click, then release.
      setTimeout(() => { busy = false; }, 3000);
    }
  };
  const _enhancePlay = async () => {
    // The <video> src often appears only once playback has started, so resolve
    // with a short retry before giving up.
    let rawUrl = currentVideoURL();
    for (let i = 0; i < 12 && !rawUrl; i++) {
      await new Promise((r) => setTimeout(r, 300));
      rawUrl = currentVideoURL();
    }
    if (!rawUrl) {
      toast("未找到视频源,请先让视频开始播放", 4000);
      return;
    }
    const url = new URL(rawUrl, location.href).href;
    log("enhance play:", url);
    toast("启动增强管线…");

    // The scheme was already fired in the click handler (within the gesture).
    // Wait for the ingest server to come up.
    if (!(await serverReady())) {
      toast("ytplay 没启动,YtplayScheme.app 装了吗?", 5000);
      return;
    }
    toast("下载中…");

    // Push one fixed-size chunk as a NORMAL POST (metadata in the query string,
    // no custom headers, body is a sized ArrayBuffer -> Content-Length set). A
    // streaming ReadableStream body only works over HTTP/2; our local server is
    // HTTP/1.1, where Chrome refuses it with "Failed to fetch".
    const pushChunk = async (buf, offset, total, final) => {
      const u = `${INGEST}/ingest?offset=${offset}&total=${total}${final ? "&final=1" : ""}`;
      const r = await fetch(u, { method: "POST", body: buf });
      if (!r.ok) throw new Error("ingest POST " + r.status);
    };

    try {
      // 2. blob: src = already in memory -> push it whole
      if (url.startsWith("blob:")) {
        const buf = new Uint8Array(await (await fetch(url)).arrayBuffer());
        await pushChunk(buf, 0, buf.length, true);
        log("pushed blob", buf.length, "bytes");
        return;
      }

      // 3. sized Range download
      const { size, ranged } = await probeSize(url);
      if (!ranged || !size) {
        const buf = new Uint8Array(await (await fetch(url)).arrayBuffer());
        await pushChunk(buf, 0, buf.length, true);
        log("pushed (no-range)", buf.length, "bytes");
        return;
      }
      log(`size ${(size / 1e6).toFixed(1)} MB, ${LANES} lanes x ${CHUNK >> 20}MB`);

      const nChunks = Math.ceil(size / CHUNK);
      const ready = new Map();        // index -> Uint8Array (downloaded, awaiting push)
      let nextToClaim = 0, pushPos = 0;
      let waiterIdx = -1, waiterResolve = null;
      const WINDOW = LANES * 3;        // bound read-ahead (memory) for big files

      // Fetch EXACTLY the [i*CHUNK, +CHUNK) window. Telegram caps each Range
      // response at ~512KB, so one request can return fewer bytes than asked -
      // keep requesting the remainder until the window is full (or EOF).
      const fetchChunk = async (i) => {
        const start = i * CHUNK;
        const stop = Math.min(start + CHUNK, size);   // exclusive
        const parts = [];
        let pos = start;
        while (pos < stop) {
          let got = null;
          for (let attempt = 0; attempt < 5; attempt++) {
            try {
              const r = await fetch(url, { headers: { Range: `bytes=${pos}-${stop - 1}` } });
              if (![200, 206].includes(r.status)) throw new Error("status " + r.status);
              got = new Uint8Array(await r.arrayBuffer());
              break;
            } catch (e) {
              if (attempt === 4) throw e;
              await new Promise((res) => setTimeout(res, 300 * (attempt + 1)));
            }
          }
          if (!got || got.length === 0) break;   // no progress -> EOF
          parts.push(got); pos += got.length;
        }
        if (parts.length === 1) return parts[0];
        const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
        let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
        return out;
      };

      // parallel download lanes fill `ready` ahead of the sequential pusher,
      // bounded by WINDOW so a 3.7GB file doesn't buffer unboundedly in memory.
      const lane = async () => {
        while (true) {
          while (nextToClaim - pushPos > WINDOW) {
            await new Promise((res) => setTimeout(res, 20));
          }
          const i = nextToClaim++;
          if (i >= nChunks) return;
          ready.set(i, await fetchChunk(i));
          if (i === waiterIdx && waiterResolve) {
            const r = waiterResolve; waiterResolve = null; r();
          }
        }
      };
      const lanesDone = Promise.all(
        Array.from({ length: Math.min(LANES, nChunks) }, lane));

      // in-order pusher: POST chunk i once downloaded, waiting if a lane is behind
      for (let i = 0; i < nChunks; i++) {
        pushPos = i;
        while (!ready.has(i)) {
          await new Promise((res) => {
            waiterIdx = i; waiterResolve = res;
            if (ready.has(i)) { waiterResolve = null; res(); }
          });
        }
        const data = ready.get(i); ready.delete(i);
        await pushChunk(data, i * CHUNK, size, i === nChunks - 1);
      }
      await lanesDone;
      log("download + push complete");
      toast("已送入增强,SenPlayer 即将打开", 5000);
    } catch (e) {
      log("ingest push failed:", e);
      toast("推流失败: " + e.message, 6000);
    }
  };

  // --- resolve the open media-viewer video's URL, re-queried fresh at click ---
  // Telegram has two web clients with different DOM (K = /k/, the default;
  // A/Z = /a/). Try both; read the live <video> (K uses .src, A/Z currentSrc).
  const currentVideoURL = () => {
    // A/Z client (/a/): the ACTIVE slide's video holds the real src; the other
    // (preloaded) slides have empty src, so scope to --active.
    const av = document.querySelector(".MediaViewerSlide--active video.media-viewer-video")
      || document.querySelector(".MediaViewerSlide--active .VideoPlayer video");
    if (av) {
      const u = av.currentSrc || av.src || av.querySelector("source")?.src;
      if (u) return u;
    }
    // K client (/k/)
    const kv = document.querySelector(
      ".media-viewer-whole .media-viewer-aspecter video");
    if (kv) {
      const u = kv.src || kv.currentSrc || kv.querySelector("source")?.src;
      if (u) return u;
    }
    // Generic fallback: any video with a real (non-blob) http(s) src.
    for (const v of document.querySelectorAll("video")) {
      const u = v.currentSrc || v.src;
      if (u && /^https?:/.test(u)) return u;
    }
    return null;
  };

  // --- button injection (mirrors the Telegram Media Downloader hooks) ------
  const ICON = "✨";
  const makeBtn = () => {
    const b = document.createElement("button");
    // Classes for BOTH clients so it fits either control bar; harmless extras.
    b.className = "tel-enhance Button smaller translucent-white round "
      + "btn-icon default__button";
    b.type = "button";
    b.title = "Enhance Play (ytplay)";
    b.setAttribute("aria-label", "Enhance Play");
    b.textContent = ICON;
    b.style.fontSize = "18px";
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      // Fire the scheme NOW, inside the user gesture (external-protocol launch is
      // gesture-gated; an await first would lose the gesture and the launch).
      fireScheme(`ytplay://ingest?enhance=${ENHANCE}&player=${PLAYER}`);
      enhancePlay();   // resolves the source + pushes (async)
    };
    return b;
  };

  setInterval(() => {
    // --- webk "K" client (default: web.telegram.org/k/) ---
    const kWhole = document.querySelector(".media-viewer-whole");
    if (kWhole) {
      const kAspect = kWhole.querySelector(".media-viewer-movers .media-viewer-aspecter");
      const kRight = kAspect && kAspect.querySelector(
        ".ckin__player .default__controls.ckin__controls .bottom-controls .right-controls");
      if (kRight && !kRight.querySelector("button.tel-enhance")) {
        kRight.prepend(makeBtn());
      }
      // topbar fallback (video present but player controls not up yet)
      const kBar = kWhole.querySelector(".media-viewer-topbar .media-viewer-buttons");
      if (kBar && !kBar.querySelector("button.tel-enhance")
          && kAspect && kAspect.querySelector("video")) {
        kBar.prepend(makeBtn());
      }
    }
    // --- webz "A/Z" client (web.telegram.org/a/) ---
    const aPlayer = document.querySelector(".MediaViewerSlide--active .VideoPlayer")
      || document.querySelector("#MediaViewer .VideoPlayer");
    if (aPlayer) {
      const controls = aPlayer.querySelector(".VideoPlayerControls .buttons");
      if (controls && !controls.querySelector("button.tel-enhance")) {
        const btn = makeBtn();
        const spacer = controls.querySelector(".spacer");
        if (spacer) spacer.after(btn); else controls.appendChild(btn);
      }
      const actions = document.querySelector("#MediaViewer .MediaViewerActions");
      if (actions && !actions.querySelector("button.tel-enhance")) {
        actions.prepend(makeBtn());
      }
    }
  }, 800);

  log("loaded; ingest port", INGEST_PORT, "tier", ENHANCE);
})();
