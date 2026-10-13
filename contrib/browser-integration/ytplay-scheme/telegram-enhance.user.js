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
  const LANES = 8;
  const CHUNK = 4 << 20;              // 4 MB per range request

  const log = (...a) => console.log("[tg-enhance]", ...a);

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
      alert("未找到视频源。请先让视频开始播放(点一下播放),再点 ✨。");
      return;
    }
    const url = new URL(rawUrl, location.href).href;
    log("enhance play:", url);

    // 1. start the native ingest server
    location.href = `ytplay://ingest?enhance=${ENHANCE}&player=${PLAYER}`;
    if (!(await serverReady())) {
      alert("ytplay ingest server did not start.\nIs YtplayScheme.app installed?");
      return;
    }

    // 2. blob: src = already in memory -> just push it whole
    if (url.startsWith("blob:")) {
      const buf = await (await fetch(url)).arrayBuffer();
      await fetch(`${INGEST}/ingest`, {
        method: "POST", headers: { "X-Ingest-Final": "1" }, body: buf,
      });
      log("pushed blob", buf.byteLength, "bytes");
      return;
    }

    // 3. sized Range download
    const { size, ranged } = await probeSize(url);
    if (!ranged || !size) {
      // no Range -> single sequential fetch piped straight to ingest
      const resp = await fetch(url);
      await fetch(`${INGEST}/ingest`, {
        method: "POST", headers: { "X-Ingest-Final": "1" }, body: resp.body,
        duplex: "half",
      });
      log("pushed (no-range) done");
      return;
    }
    log(`size ${(size / 1e6).toFixed(1)} MB, ${LANES} lanes x ${CHUNK >> 20}MB`);

    const nChunks = Math.ceil(size / CHUNK);
    const ready = new Map();          // index -> Uint8Array (completed, awaiting order)
    let nextToClaim = 0;              // next chunk index a lane may fetch
    let nextToEmit = 0;              // next chunk index the writer must emit
    let emitResolve = null;           // wake the writer when a needed chunk lands

    const fetchChunk = async (i) => {
      const start = i * CHUNK;
      const end = Math.min(start + CHUNK, size) - 1;
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const r = await fetch(url, { headers: { Range: `bytes=${start}-${end}` } });
          if (![200, 206].includes(r.status)) throw new Error("status " + r.status);
          return new Uint8Array(await r.arrayBuffer());
        } catch (e) {
          if (attempt === 4) throw e;
          await new Promise((res) => setTimeout(res, 300 * (attempt + 1)));
        }
      }
    };

    // lanes: each grabs the next unclaimed chunk until exhausted
    const lane = async () => {
      while (true) {
        const i = nextToClaim++;
        if (i >= nChunks) return;
        const data = await fetchChunk(i);
        ready.set(i, data);
        if (i === nextToEmit && emitResolve) { const r = emitResolve; emitResolve = null; r(); }
      }
    };
    const lanes = Array.from({ length: Math.min(LANES, nChunks) }, lane);
    const lanesDone = Promise.all(lanes);

    // writer: a ReadableStream that emits chunks strictly in order as they land
    const body = new ReadableStream({
      async pull(controller) {
        while (!ready.has(nextToEmit)) {
          if (nextToEmit >= nChunks) { controller.close(); return; }
          // Register the waiter, then re-check synchronously inside the executor:
          // a lane may have landed nextToEmit between the while-check and here,
          // which would otherwise hang the writer forever.
          await new Promise((res) => {
            emitResolve = res;
            if (ready.has(nextToEmit)) { emitResolve = null; res(); }
          });
        }
        const data = ready.get(nextToEmit);
        ready.delete(nextToEmit);
        controller.enqueue(data);
        nextToEmit++;
        if (nextToEmit >= nChunks) controller.close();
      },
    });

    const post = fetch(`${INGEST}/ingest`, {
      method: "POST", headers: { "X-Ingest-Final": "1" }, body, duplex: "half",
    });
    try {
      await Promise.all([lanesDone, post]);
      log("download + push complete");
    } catch (e) {
      log("ingest push failed:", e);
      alert("Enhance play failed: " + e.message);
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
      enhancePlay();   // resolves the source fresh (with retry) at click time
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
