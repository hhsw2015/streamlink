// Extractor Worker: platform URL → direct download URL.
// See docs/extractor.md for API spec.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "X-Auth, Content-Type",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, {status: 204, headers: CORS});
    try {
      const r = await route(request, env, ctx);
      for (const [k, v] of Object.entries(CORS)) r.headers.set(k, v);
      return r;
    } catch (e) {
      return json({error: String(e), stack: e.stack?.slice(0, 500)}, 500);
    }
  },
  async scheduled(event, env, ctx) {
    await scheduled(env, ctx);
  },
};

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const path = url.pathname;

  // Auth (skip for /healthz)
  if (path !== "/healthz") {
    if (request.headers.get("X-Auth") !== env.AUTH_TOKEN) {
      return json({error: "unauthorized"}, 401);
    }
  }

  if (path === "/healthz") return json({ok: true, ts: Date.now()});
  if (path === "/extract" && request.method === "POST") return handleExtract(request, env, ctx);
  if (path.startsWith("/status/")) return handleStatus(path.slice(8), env, ctx);
  if (path.startsWith("/result/")) return handleResult(path.slice(8), env);
  if (path.startsWith("/advance/") && request.method === "POST") return handleAdvance(path.slice(9), env, ctx);
  if (path.startsWith("/job/") && request.method === "DELETE") return handleDelete(path.slice(5), env);
  if (path === "/services") return handleServices(env);
  if (path === "/jobs") return handleJobs(url, env);

  return json({error: "not found"}, 404);
}

// ── JSON helper ──────────────────────────────────────────
function json(o, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(o), {
    status,
    headers: {"Content-Type": "application/json", ...extraHeaders},
  });
}

// ── Quality canonicalization ─────────────────────────────
// Normalize varied strings ("1080p", "1080", "1080p Full HD", "FHD") → "1080p".
// Special values pass through: "best" / "smallest" / "audio_only".
function canonQuality(q) {
  const s = String(q || "best").toLowerCase().trim();
  if (s === "best" || s === "smallest" || s === "audio_only") return s;
  if (s === "fhd") return "1080p";
  if (s === "qhd" || s === "2k") return "1440p";
  if (s === "uhd" || s === "4k") return "2160p";
  if (s === "hd") return "720p";
  if (s === "sd") return "480p";
  const m = s.match(/(\d{3,4})/);
  return m ? `${m[1]}p` : "best";
}

// ── URL normalization ────────────────────────────────────
// Strip tracking params, resolve short links, unify formats.
// Same underlying video → same normalized URL → cache hit.
function normalizeUrl(raw) {
  if (!raw) return raw;
  let u;
  try { u = new URL(raw.trim()); } catch { return raw.trim(); }
  const host = u.hostname.toLowerCase().replace(/^www\./, "").replace(/^m\./, "");

  // YouTube: https://www.youtube.com/watch?v=ID or youtu.be/ID or shorts/ID
  if (host === "youtube.com" || host === "youtu.be") {
    let id = "";
    if (host === "youtu.be") id = u.pathname.slice(1);
    else if (u.pathname === "/watch") id = u.searchParams.get("v") || "";
    else if (u.pathname.startsWith("/shorts/")) id = u.pathname.slice("/shorts/".length);
    else if (u.pathname.startsWith("/embed/")) id = u.pathname.slice("/embed/".length);
    id = id.split("/")[0].split("?")[0];
    if (id) return `https://www.youtube.com/watch?v=${id}`;
  }
  // Bilibili: BV / av / short b23.tv, keep ?p= for multi-part videos
  if (host === "bilibili.com" || host === "b23.tv") {
    const m = u.pathname.match(/\/video\/(BV[\w]+|av\d+)/i);
    if (m) {
      const p = u.searchParams.get("p");
      return `https://www.bilibili.com/video/${m[1]}` + (p && p !== "1" ? `?p=${p}` : "");
    }
  }
  // TikTok: strip query params (share_id etc)
  if (host === "tiktok.com" || host === "vm.tiktok.com" || host === "vt.tiktok.com") {
    return `${u.protocol}//${host}${u.pathname}`;
  }
  // X/Twitter: keep /status/ID only
  if (host === "twitter.com" || host === "x.com") {
    const m = u.pathname.match(/\/status\/(\d+)/);
    if (m) return `https://x.com${u.pathname.split("/status/")[0]}/status/${m[1]}`;
  }
  // Instagram: /reel/ID or /p/ID
  if (host === "instagram.com") {
    const m = u.pathname.match(/\/(reel|p|tv)\/([^\/]+)/);
    if (m) return `https://www.instagram.com/${m[1]}/${m[2]}/`;
  }
  // 抖音 douyin: /video/ID
  if (host === "douyin.com" || host === "v.douyin.com") {
    const m = u.pathname.match(/\/video\/(\d+)/);
    if (m) return `https://www.douyin.com/video/${m[1]}`;
  }
  // 小红书 xiaohongshu: /explore/ID
  if (host === "xiaohongshu.com" || host === "xhslink.com") {
    const m = u.pathname.match(/\/explore\/([^\/]+)/);
    if (m) return `https://www.xiaohongshu.com/explore/${m[1]}`;
  }

  // Generic: drop hash + common tracking params
  const clean = new URL(u.origin + u.pathname);
  const skipParams = new Set(["utm_source","utm_medium","utm_campaign","utm_term","utm_content",
                              "si","t","feature","fbclid","gclid","spm","share_source","share_token"]);
  for (const [k, v] of u.searchParams) {
    if (!skipParams.has(k.toLowerCase())) clean.searchParams.set(k, v);
  }
  return clean.toString();
}

// ── /extract ─────────────────────────────────────────────
async function handleExtract(request, env, ctx) {
  const body = await request.json().catch(() => ({}));
  const raw_url = body.source_url;
  const source_url = normalizeUrl(raw_url);
  const {service_hint, webhook, webhook_headers, meta, no_cache} = body;
  const quality = canonQuality(body.quality);  // Canonicalize before both cache lookup and INSERT
  if (!source_url) return json({error: "source_url required"}, 400);

  // Cache check: reuse a live success job for the same {source_url, quality}
  // unless caller passed no_cache: true.
  if (!no_cache) {
    let cached = null;
    try {
      cached = await env.DB.prepare(`
        SELECT * FROM extractor_jobs
        WHERE source_url = ? AND quality = ? AND status = 'success'
          AND expires_at IS NOT NULL AND expires_at > ?
        ORDER BY created_at DESC LIMIT 1
      `).bind(source_url, quality, Date.now() + 300000).first();  // 5-min buffer for downloads
    } catch (_) {
      // D1 hiccup — fall through to normal flow
    }

    if (cached) {
      // Inline full result so caller doesn't need a second /result/:id call.
      let required_headers = {};
      try { required_headers = JSON.parse(cached.required_headers || "{}"); } catch (_) {}

      // Fire caller's webhook if provided (so caller doesn't wait forever).
      if (webhook) {
        const virtualJob = {
          ...cached,
          id: cached.id,
          webhook,
          webhook_headers: JSON.stringify(webhook_headers || {}),
          meta: JSON.stringify(meta || {}),
        };
        ctx.waitUntil(fireWebhook(virtualJob));
      }

      return json({
        job_id: cached.id,
        status: "success",
        cached: true,
        cached_age_s: Math.floor((Date.now() - cached.created_at) / 1000),
        direct_url: cached.direct_url,
        file_size: cached.file_size,
        filename: cached.filename,
        title: cached.title,
        platform: cached.platform,
        format: cached.format,
        quality: cached.quality_actual,
        supports_range: cached.supports_range === 1 ? true : cached.supports_range === 0 ? false : null,
        required_headers,
        expires_at: cached.expires_at,
        created_at: cached.created_at,
      });
    }
  }

  const service = await selectService(env, source_url, service_hint);
  if (!service) {
    return json({error: "no upstream can handle this URL (or all cooldown)"}, 503, {"Retry-After": "300"});
  }

  const jobId = crypto.randomUUID();
  const now = Date.now();
  await env.DB.prepare(`
    INSERT INTO extractor_jobs
      (id, created_at, updated_at, source_url, quality, service_hint, webhook,
       webhook_headers, meta, service, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
  `).bind(
    jobId, now, now, source_url, quality, service_hint || null,
    webhook || null, JSON.stringify(webhook_headers || {}),
    JSON.stringify(meta || {}), service.id,
  ).run();

  // Async advance: return immediately, background nudge extract+submit.
  ctx.waitUntil(nudgeJob(jobId, env, ctx));

  return json({job_id: jobId, status: "pending", created_at: now});
}

// ── /status/:id ──────────────────────────────────────────
// Side-effect: if job is non-terminal and stale, kick a nudge in the background.
// This makes client polling act as a natural accelerator, complementing cron.
async function handleStatus(id, env, ctx) {
  const job = await env.DB.prepare("SELECT * FROM extractor_jobs WHERE id=?").bind(id).first();
  if (!job) return json({error: "job not found"}, 404);
  const nonTerminal = job.status === "pending" || job.status === "polling";
  const stale = Date.now() - (job.updated_at || 0) > 1000;  // 1s idle → re-nudge
  if (nonTerminal && stale) {
    ctx.waitUntil(nudgeJob(id, env, ctx));
  }
  return json({
    job_id: job.id,
    status: job.status,
    progress: job.ext_progress || 0,
    service: job.service,
    ext_task_id: job.ext_task_id,
    ext_status: job.ext_status,
    message: job.message,
    error: job.error,
    created_at: job.created_at,
    updated_at: job.updated_at,
  });
}

// ── /advance/:id ─────────────────────────────────────────
async function handleAdvance(id, env, ctx) {
  ctx.waitUntil(nudgeJob(id, env, ctx));
  return json({ok: true, kicked: id});
}

// ── /result/:id ──────────────────────────────────────────
async function handleResult(id, env) {
  const job = await env.DB.prepare("SELECT * FROM extractor_jobs WHERE id=?").bind(id).first();
  if (!job) return json({error: "job not found"}, 404);
  if (job.status === "failed") return json({status: "failed", error: job.error || "unknown"}, 500);
  if (job.status !== "success") return json({status: job.status, error: "not ready yet"}, 409);
  if (job.expires_at && Date.now() > job.expires_at) {
    return json({error: "direct URL expired", expires_at: job.expires_at}, 410);
  }
  let required_headers = {};
  try { required_headers = JSON.parse(job.required_headers || "{}"); } catch (_) {}
  return json({
    job_id: job.id,
    status: "success",
    direct_url: job.direct_url,
    file_size: job.file_size,
    filename: job.filename,
    title: job.title,
    platform: job.platform,
    format: job.format,
    quality: job.quality_actual,
    duration_s: job.duration_s,
    supports_range: job.supports_range === 1 ? true : job.supports_range === 0 ? false : null,
    required_headers,
    expires_at: job.expires_at,
    created_at: job.created_at,
  });
}

// ── /job/:id DELETE ──────────────────────────────────────
async function handleDelete(id, env) {
  await env.DB.prepare("DELETE FROM extractor_jobs WHERE id=?").bind(id).run();
  return new Response(null, {status: 204});
}

// ── /services ────────────────────────────────────────────
async function handleServices(env) {
  const rows = await env.DB.prepare("SELECT * FROM upstream_services").all();
  const services = (rows.results || []).map(s => ({
    id: s.id,
    base_url: s.base_url,
    api_type: s.api_type,
    platforms: safeJson(s.supported_platforms, ["*"]),
    weight: s.weight,
    enabled: !!s.enabled,
    last_429_at: s.last_429_at || 0,
    consecutive_429: s.consecutive_429 || 0,
    direct_url_ttl_s: s.direct_url_ttl_s,
    status: cooldownActive(s) ? "cooldown" : (s.enabled ? "healthy" : "disabled"),
  }));
  return json({services});
}

// ── /jobs ────────────────────────────────────────────────
async function handleJobs(url, env) {
  const status = url.searchParams.get("status");
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 500);
  let q = "SELECT id, status, source_url, service, created_at, updated_at, error FROM extractor_jobs";
  const args = [];
  if (status) {
    q += " WHERE status = ?";
    args.push(status);
  }
  q += " ORDER BY updated_at DESC LIMIT ?";
  args.push(limit);
  const rows = await env.DB.prepare(q).bind(...args).all();
  return json({jobs: rows.results || []});
}

// ── selectService (balancer) ─────────────────────────────
function safeJson(s, def) { try { return JSON.parse(s); } catch (_) { return def; } }

function cooldownActive(svc) {
  const consec = svc.consecutive_429 || 0;
  if (consec === 0) return false;
  // Exponential: 5m, 15m, 1h, 6h, disable
  const stages = [5, 15, 60, 360];
  const idx = Math.min(consec - 1, stages.length - 1);
  const cooldownMs = stages[idx] * 60 * 1000;
  return Date.now() - (svc.last_429_at || 0) < cooldownMs;
}

function detectPlatform(url) {
  const u = url.toLowerCase();
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
  if (u.includes("bilibili.com") || u.includes("b23.tv")) return "bilibili";
  if (u.includes("douyin.com")) return "douyin";
  if (u.includes("tiktok.com")) return "tiktok";
  if (u.includes("twitter.com") || u.includes("x.com")) return "x";
  if (u.includes("instagram.com")) return "instagram";
  if (u.includes("facebook.com") || u.includes("fb.watch")) return "facebook";
  if (u.includes("threads.net")) return "threads";
  if (u.includes("reddit.com")) return "reddit";
  if (u.includes("pinterest.com")) return "pinterest";
  if (u.includes("vimeo.com")) return "vimeo";
  if (u.includes("snapchat.com")) return "snapchat";
  if (u.includes("xiaohongshu.com") || u.includes("xhslink.com")) return "xiaohongshu";
  if (u.includes("weibo.com") || u.includes("weibo.cn")) return "weibo";
  if (u.includes("kuaishou.com")) return "kuaishou";
  return "unknown";
}

async function selectService(env, sourceURL, hint) {
  const platform = detectPlatform(sourceURL);
  const rows = await env.DB.prepare("SELECT * FROM upstream_services WHERE enabled=1").all();
  let candidates = (rows.results || []).filter(s => {
    if (hint && s.id !== hint) return false;
    const sup = safeJson(s.supported_platforms, []);
    return sup.includes(platform) || sup.includes("*");
  });
  // Prefer not in cooldown
  const notCooling = candidates.filter(s => !cooldownActive(s));
  if (notCooling.length) candidates = notCooling;
  if (!candidates.length) return null;

  // Weighted random
  const total = candidates.reduce((s, c) => s + (c.weight || 1), 0);
  let r = Math.random() * total;
  for (const c of candidates) {
    r -= (c.weight || 1);
    if (r <= 0) return c;
  }
  return candidates[candidates.length - 1];
}

async function getService(env, id) {
  return env.DB.prepare("SELECT * FROM upstream_services WHERE id=?").bind(id).first();
}

// ── Upstream adapter (vthreads / cobalt / metube) ────────
async function callUpstream(service, action, params) {
  const headers = safeJson(service.required_headers, {});
  if (service.api_type === "vthreads") return callVthreads(service, action, params, headers);
  if (service.api_type === "cobalt")  return callCobalt(service, action, params, headers);
  if (service.api_type === "metube")  return callMetube(service, action, params, headers);
  throw new Error(`unsupported api_type: ${service.api_type}`);
}

async function callVthreads(service, action, params, headers) {
  if (action === "extract") {
    const u = `${service.base_url}/api/extract?url=${encodeURIComponent(params.url)}&lang=zh`;
    const r = await fetch(u, {headers});
    if (r.status === 429) throw new RateLimitError(`vthreads 429 on extract`);
    if (!r.ok) throw new Error(`vthreads extract HTTP ${r.status}`);
    return await r.json();
  }
  if (action === "submit") {
    const u = `${service.base_url}${params.rel_url}`;
    const r = await fetch(u, {headers});
    if (r.status === 429) throw new RateLimitError(`vthreads 429 on submit`);
    if (!r.ok) throw new Error(`vthreads submit HTTP ${r.status}`);
    return await r.json();
  }
  if (action === "status") {
    const u = `${service.base_url}/api/check_status/${params.task_id}`;
    const r = await fetch(u, {headers});
    if (r.status === 429) throw new RateLimitError(`vthreads 429 on status`);
    if (!r.ok) throw new Error(`vthreads status HTTP ${r.status}`);
    return await r.json();
  }
  throw new Error(`unknown vthreads action: ${action}`);
}

async function callCobalt(service, action, params, headers) {
  // cobalt has a POST /api/json single-shot endpoint.
  // We map extract+submit → one call, status → HEAD probe of tunnel.
  if (action === "extract" || action === "submit") {
    const r = await fetch(`${service.base_url}/api/json`, {
      method: "POST",
      headers: {...headers, "Content-Type": "application/json", "Accept": "application/json"},
      body: JSON.stringify({url: params.url || params.orig_url, videoQuality: params.quality || "1080"}),
    });
    if (r.status === 429) throw new RateLimitError(`cobalt 429`);
    if (!r.ok) throw new Error(`cobalt HTTP ${r.status}`);
    const d = await r.json();
    if (d.status === "error" || d.status === "rate-limit") {
      if (d.status === "rate-limit") throw new RateLimitError(d.text || "cobalt rate-limit");
      throw new Error(d.text || "cobalt error");
    }
    if (d.status === "stream" || d.status === "redirect") {
      // stream: cobalt proxy URL; redirect: origin URL (YouTube signed).
      return {
        _cobalt_final: true,
        direct_url: d.url,
        filename: d.filename,
        supports_range: d.status === "stream", // origin URLs may not honor Range consistently
      };
    }
    if (d.status === "tunnel" || d.status === "local-processing") {
      return {task_id: d.url, tunnel: true};
    }
    if (d.status === "picker") {
      // multi-media page (e.g. album). Pick first.
      const first = (d.picker || [])[0];
      if (!first) throw new Error("cobalt picker empty");
      return {_cobalt_final: true, direct_url: first.url, filename: first.filename};
    }
    throw new Error(`cobalt unknown status: ${d.status}`);
  }
  if (action === "status") {
    // For cobalt tunnel: HEAD tunnel URL; success only if Content-Length known.
    const r = await fetch(params.task_id, {method: "HEAD", headers});
    const cl = r.headers.get("Content-Length");
    if (r.ok && cl && parseInt(cl, 10) > 0) {
      return {status: "SUCCESS", download_url: params.task_id, file_size: parseInt(cl, 10)};
    }
    return {status: "PENDING"};
  }
  throw new Error(`unknown cobalt action: ${action}`);
}

async function callMetube(service, action, params, headers) {
  // Self-hosted metube: assume vthreads-like API contract on top (custom bridge).
  // First cut: reuse vthreads adapter shape.
  return callVthreads(service, action, params, headers);
}

class RateLimitError extends Error {
  constructor(m) { super(m); this.name = "RateLimitError"; }
}

// ── nudgeJob (state machine, one step per invocation) ────
async function nudgeJob(jobId, env, ctx) {
  const workerUuid = crypto.randomUUID();
  const now = Date.now();
  const leased = await env.DB.prepare(`
    UPDATE extractor_jobs SET owner_id=?, owner_expires=?
    WHERE id=? AND (owner_id IS NULL OR owner_expires < ?)
  `).bind(workerUuid, now + 60000, jobId, now).run();
  if (!leased.meta || !leased.meta.changes) return; // didn't get lease

  let job;
  try {
    job = await env.DB.prepare("SELECT * FROM extractor_jobs WHERE id=?").bind(jobId).first();
    if (!job) return;
    const service = await getService(env, job.service);
    if (!service) throw new Error(`unknown service ${job.service}`);

    if (job.status === "pending") {
      // extract + submit in one go.
      const ext = await callUpstream(service, "extract", {url: job.source_url});

      // cobalt one-shot short-circuit
      if (ext && ext._cobalt_final) {
        job.title = job.source_url.slice(0, 100);
        job.platform = detectPlatform(job.source_url);
        job.filename = ext.filename || sanitize(job.title) + ".mp4";
        job.direct_url = ext.direct_url;
        job.supports_range = ext.supports_range !== false ? 1 : 0;
        job.expires_at = now + (service.direct_url_ttl_s || 3600) * 1000;
        job.status = "success";
        job.required_headers = service.required_headers;
        await saveJob(env, job);
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
        return;
      }

      // vthreads path
      if (!ext.success) throw new Error(`upstream extract failed: ${JSON.stringify(ext).slice(0,200)}`);
      const medias = ext.data?.medias || [];
      if (!medias.length) throw new Error("upstream returned no medias");
      const media = pickQuality(medias, job.quality);
      if (!media) throw new Error("no matching quality");

      const sub = await callUpstream(service, "submit", {rel_url: media.url});
      if (!sub.task_id) throw new Error(`upstream submit no task_id: ${JSON.stringify(sub).slice(0,200)}`);

      job.ext_task_id = sub.task_id;
      job.title = ext.data.title || "";
      job.platform = ext.data.platform || detectPlatform(job.source_url);
      job.filename = sanitize(job.title || job.source_url) + guessExt(job.platform);
      job.format = "mp4";   // upstream (vthreads) always merges to mp4
      job.quality_actual = media.quality;
      job.status = "polling";
      job.message = "submitted, waiting upstream to prepare file";
      await saveJob(env, job);
      return;
    }

    if (job.status === "polling") {
      const st = await callUpstream(service, "status", {task_id: job.ext_task_id});
      job.ext_status = st.status;
      job.ext_progress = st.progress || 0;
      job.message = `upstream ${st.status}${st.progress != null ? " " + st.progress + "%" : ""}`;
      if (st.status === "SUCCESS") {
        // Compose direct URL (vthreads returns path; cobalt returns absolute)
        let du = st.download_url || "";
        if (du && !du.startsWith("http")) du = service.base_url + du;
        job.direct_url = du;
        job.file_size = st.file_size || null;
        job.supports_range = st.supports_range === false ? 0 : 1;
        job.required_headers = service.required_headers;
        job.expires_at = now + (service.direct_url_ttl_s || 3600) * 1000;
        job.status = "success";
        await saveJob(env, job);
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
        return;
      }
      if (st.status === "FAILED") {
        job.status = "failed";
        job.error = "upstream reported FAILED";
        await saveJob(env, job);
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
        return;
      }
      // Still working — save progress and let next tick continue.
      await saveJob(env, job);
      return;
    }
    // terminal: nothing to do
  } catch (err) {
    if (job) {
      if (err instanceof RateLimitError) {
        await markService429(env, job.service);
      }
      job.retry_count = (job.retry_count || 0) + 1;
      job.error = String(err).slice(0, 500);
      if (job.retry_count >= 3) {
        job.status = "failed";
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
      }
      await saveJob(env, job);
    }
  } finally {
    await env.DB.prepare(
      "UPDATE extractor_jobs SET owner_id=NULL, owner_expires=NULL WHERE id=?"
    ).bind(jobId).run();
  }
}

async function saveJob(env, job) {
  const now = Date.now();
  await env.DB.prepare(`
    UPDATE extractor_jobs SET
      updated_at=?, status=?, ext_task_id=?, ext_status=?, ext_progress=?, message=?, error=?,
      retry_count=?, direct_url=?, file_size=?, filename=?, title=?, platform=?, format=?,
      quality_actual=?, duration_s=?, required_headers=?, supports_range=?, expires_at=?
    WHERE id=?
  `).bind(
    now, job.status, job.ext_task_id, job.ext_status, job.ext_progress || 0, job.message, job.error,
    job.retry_count || 0, job.direct_url, job.file_size, job.filename, job.title, job.platform, job.format,
    job.quality_actual, job.duration_s, job.required_headers, job.supports_range, job.expires_at,
    job.id,
  ).run();
}

async function markService429(env, id) {
  await env.DB.prepare(`
    UPDATE upstream_services
    SET last_429_at = ?, consecutive_429 = consecutive_429 + 1
    WHERE id = ?
  `).bind(Date.now(), id).run();
}

// ── Quality picking ──────────────────────────────────────
function pickQuality(medias, preference) {
  const pref = (preference || "best").toLowerCase();
  const scored = medias.map(m => ({m, s: qualityScore(m)}));
  if (pref === "best") {
    scored.sort((a, b) => b.s - a.s);
    return scored[0]?.m;
  }
  if (pref === "smallest") {
    // Prefer smallest size, then lowest quality
    const withSize = scored.filter(x => x.m.size);
    if (withSize.length) {
      withSize.sort((a, b) => parseSize(a.m.size) - parseSize(b.m.size));
      return withSize[0].m;
    }
    scored.sort((a, b) => a.s - b.s);
    return scored[0]?.m;
  }
  if (pref === "audio_only") {
    return medias.find(m => m.quality?.toLowerCase().includes("audio")) || null;
  }
  // Exact/partial match: "1080p", "720p", etc.
  const exact = scored.find(x => (x.m.quality || "").toLowerCase().includes(pref));
  if (exact) return exact.m;
  // Fallback: pick closest below
  const target = qualityNum(pref);
  const below = scored.filter(x => x.s <= target).sort((a, b) => b.s - a.s);
  if (below.length) return below[0].m;
  scored.sort((a, b) => b.s - a.s);
  return scored[0]?.m;
}

function qualityScore(m) {
  const q = (m.quality || "").toLowerCase();
  if (q.includes("4k") || q.includes("2160")) return 2160;
  if (q.includes("2k") || q.includes("1440")) return 1440;
  if (q.includes("1080")) return 1080;
  if (q.includes("720")) return 720;
  if (q.includes("480")) return 480;
  if (q.includes("360")) return 360;
  if (q.includes("240")) return 240;
  if (q.includes("audio")) return 0;
  return 100;
}
function qualityNum(pref) {
  const m = pref.match(/(\d{3,4})/);
  return m ? parseInt(m[1], 10) : 720;
}
function parseSize(s) {
  const m = String(s).match(/([\d.]+)\s*(K|M|G)?B?/i);
  if (!m) return Infinity;
  const n = parseFloat(m[1]);
  const u = (m[2] || "").toUpperCase();
  return u === "G" ? n * 1e9 : u === "M" ? n * 1e6 : u === "K" ? n * 1e3 : n;
}

// ── Filename sanitize ────────────────────────────────────
function sanitize(s) {
  return (s || "").replace(/[\/\\:*?"<>|]/g, "_").replace(/\s+/g, " ").trim().slice(0, 200);
}
function guessExt(platform, format) {
  // vthreads-based upstream always outputs mp4 (ffmpeg merge).
  // Future: audio_only → .m4a; other extractors → detect from mimeType.
  return ".mp4";
}

// ── Webhook ──────────────────────────────────────────────
async function fireWebhook(job) {
  const url = job.webhook;
  if (!url) return;
  const extraHeaders = safeJson(job.webhook_headers, {});
  const body = JSON.stringify({
    job_id: job.id,
    status: job.status,
    source_url: job.source_url,
    direct_url: job.direct_url,
    filename: job.filename,
    file_size: job.file_size,
    title: job.title,
    platform: job.platform,
    required_headers: safeJson(job.required_headers, {}),
    meta: safeJson(job.meta, {}),
    error: job.error,
    expires_at: job.expires_at,
  });
  const attempts = [0, 5000, 30000];
  for (let i = 0; i < attempts.length; i++) {
    if (attempts[i] > 0) await sleep(attempts[i]);
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json", ...extraHeaders},
        body,
      });
      if (r.ok) return;
    } catch (_) {}
  }
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── scheduled (cron) ─────────────────────────────────────
async function scheduled(env, ctx) {
  const now = Date.now();
  const stuck = await env.DB.prepare(`
    SELECT id FROM extractor_jobs
    WHERE status IN ('pending','polling')
      AND (owner_id IS NULL OR owner_expires < ?)
    ORDER BY updated_at ASC
    LIMIT 10
  `).bind(now).all();
  await Promise.all((stuck.results || []).map(row =>
    ctx.waitUntil(nudgeJob(row.id, env, ctx))
  ));

  // GC: soft-delete expired direct_urls (mark status if needed).
  // Hard-delete rows older than 7 days regardless of status.
  await env.DB.prepare(
    "DELETE FROM extractor_jobs WHERE updated_at < ?"
  ).bind(now - 7 * 24 * 3600 * 1000).run();
}
