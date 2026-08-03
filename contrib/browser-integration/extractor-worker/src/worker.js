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

  // Auth: /healthz open. /tg-webhook uses Telegram's own secret_token header.
  // /history.html is a browser page — accepts ?token= query param instead of
  // X-Auth (which is a pain to set from a plain URL bar). Rest uses X-Auth.
  if (path === "/tg-webhook") {
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TG_WEBHOOK_SECRET) {
      return json({error: "unauthorized"}, 401);
    }
  } else if (path === "/history.html" || path.startsWith("/dl/") || path.startsWith("/archive-status/")) {
    // Browser-friendly: accept ?token= in URL as well as X-Auth header.
    const okToken = url.searchParams.get("token") === env.AUTH_TOKEN;
    const okHeader = request.headers.get("X-Auth") === env.AUTH_TOKEN;
    if (!okToken && !okHeader) {
      return new Response("unauthorized (use ?token=<AUTH_TOKEN>)", {status: 401});
    }
  } else if (path !== "/healthz" && path !== "/play") {
    if (request.headers.get("X-Auth") !== env.AUTH_TOKEN) {
      return json({error: "unauthorized"}, 401);
    }
  }

  if (path === "/healthz") return json({ok: true, ts: Date.now()});
  if (path === "/play") return handlePlayRedirect(url);
  if (path === "/extract" && request.method === "POST") return handleExtract(request, env, ctx);
  if (path === "/tg-webhook" && request.method === "POST") return handleTgWebhook(request, env, ctx);
  if (path.startsWith("/status/")) return handleStatus(path.slice(8), env, ctx);
  if (path.startsWith("/result/")) return handleResult(path.slice(8), env);
  if (path.startsWith("/advance/") && request.method === "POST") return handleAdvance(path.slice(9), env, ctx);
  if (path.startsWith("/job/") && request.method === "DELETE") return handleDelete(path.slice(5), env);
  // /archive/:id?/(complete|failed) — job id must be non-empty and URL-decoded.
  // (encodeURIComponent'd ids from history.html JS would otherwise stay
  // percent-encoded when we bind them into WHERE id = ?.)
  if (path.startsWith("/archive/") && path.endsWith("/complete") && request.method === "POST") {
    const id = decodeArchiveId(path.slice(9, -9));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveComplete(id, request, env);
  }
  if (path.startsWith("/archive/") && path.endsWith("/failed") && request.method === "POST") {
    const id = decodeArchiveId(path.slice(9, -7));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveFailed(id, request, env);
  }
  if (path.startsWith("/archive/") && path.endsWith("/heartbeat") && request.method === "POST") {
    const id = decodeArchiveId(path.slice(9, -10));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveHeartbeat(id, request, env);
  }
  if (path.startsWith("/archive/") && path.endsWith("/claim") && request.method === "POST") {
    const id = decodeArchiveId(path.slice(9, -6));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveClaim(id, env);
  }
  if (path.startsWith("/archive/") && request.method === "POST") {
    const id = decodeArchiveId(path.slice(9));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveRequest(id, request, env);
  }
  if (path === "/archive-queue" && request.method === "GET") return handleArchiveQueue(url, env);
  if (path === "/archive-candidates" && request.method === "GET") return handleArchiveCandidates(url, env);
  if (path.startsWith("/archive-status/") && request.method === "GET") {
    const id = decodeArchiveId(path.slice(16));
    if (!id) return json({error: "job id required"}, 400);
    return handleArchiveStatus(id, env);
  }
  if (path.startsWith("/dl/")) return handleDownloadRedirect(path.slice(4), url, env, ctx);
  if (path === "/history.html") return handleHistoryHtml(url, env);
  if (path === "/services") return handleServices(env);
  if (path === "/jobs") return handleJobs(url, env);
  if (path === "/proxy-ips") {
    const rows = await env.DB.prepare(
      "SELECT ip, status, cooldown_until, success_count, fail_count, dead_count, last_success_at, last_error FROM proxy_ips ORDER BY status, updated_at DESC LIMIT 200"
    ).all();
    return json({ips: rows.results || []});
  }
  if (path === "/proxy-refresh" && request.method === "POST") {
    await env.DB.prepare("DELETE FROM proxy_refresh_meta WHERE source=?").bind(PROXY_IP_SOURCE).run();
    const debug = await refreshProxyIpsDebug(env);
    return json({ok: true, debug});
  }

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
  return await createJob(body, env, ctx);
}

// Shared job-creation core. Called by /extract and /tg-webhook.
// Returns Response (json). On cache hit, includes full result inline.
async function createJob(body, env, ctx) {
  const raw_url = body.source_url;
  const source_url = normalizeUrl(raw_url);
  const {service_hint, webhook, webhook_headers, meta, no_cache} = body;
  const quality = canonQuality(body.quality);
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

  // In-flight dedupe: if the same {source_url, quality} is already pending or
  // polling, hand back its job id instead of creating a duplicate. Clients that
  // POST /extract repeatedly (retry loops, page refresh) should NOT spawn N
  // upstream submissions — that's what was slamming vthreads and draining the
  // proxy IP pool. no_cache bypasses this (rare).
  if (!no_cache) {
    try {
      const inflight = await env.DB.prepare(`
        SELECT id, status, created_at, service, ext_task_id
        FROM extractor_jobs
        WHERE source_url = ? AND quality = ? AND status IN ('pending','polling')
        ORDER BY created_at DESC LIMIT 1
      `).bind(source_url, quality).first();
      if (inflight) {
        return json({
          job_id: inflight.id,
          status: inflight.status,
          dedup: true,
          service: inflight.service,
          created_at: inflight.created_at,
        });
      }
    } catch (_) { /* D1 hiccup — fall through to normal flow */ }
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
  // Only auto-nudge if idle for a while; avoid hammering upstream on rapid polling.
  const stale = Date.now() - (job.updated_at || 0) > 5000;   // 5s idle
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
  if (id === "all") {
    // Nudge every non-terminal job (pending + polling) in parallel. Useful for
    // sweeping jobs stuck because a prior tight-poll loop exited before the
    // upstream finished. Each nudge holds its own lease, so parallel is safe.
    const rows = await env.DB.prepare(
      "SELECT id FROM extractor_jobs WHERE status IN ('pending','polling') LIMIT 50"
    ).all();
    const ids = (rows.results || []).map(r => r.id);
    ids.forEach(jid => ctx.waitUntil(nudgeJob(jid, env, ctx)));
    return json({ok: true, kicked: ids.length});
  }
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
  const services = (rows.results || []).map(s => {
    const hdrs = safeJson(s.required_headers, {});
    return {
      id: s.id,
      base_url: s.base_url,
      api_type: s.api_type,
      platforms: safeJson(s.supported_platforms, ["*"]),
      weight: s.weight,
      enabled: !!s.enabled,
      last_429_at: s.last_429_at || 0,
      consecutive_429: s.consecutive_429 || 0,
      direct_url_ttl_s: s.direct_url_ttl_s,
      required_headers_count: Object.keys(hdrs).length,  // don't expose actual header values
      status: cooldownActive(s) ? "cooldown" : (s.enabled ? "healthy" : "disabled"),
    };
  });
  return json({services});
}

// ── /dl/:id ──────────────────────────────────────────────
// Redirect straight to the stored direct_url. No re-extract, no upstream call —
// if the URL is dead (vthreads GC'd it) the user gets a plain 4xx from vthreads
// and can re-submit via /extract manually. Never triggers a fresh download.
async function handleDownloadRedirect(id, url, env, ctx) {
  const job = await env.DB.prepare(
    "SELECT direct_url FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!job) return new Response("job not found", {status: 404});
  if (!job.direct_url) return new Response("no direct_url on this job", {status: 404});
  return Response.redirect(job.direct_url, 302);
}

// Safe URI-decode: clients that call `fetch('/archive/' + encodeURIComponent(id))`
// send percent-encoded ids; unless we decode them, D1 `WHERE id = ?` won't match.
// Returns null for empty or malformed input so callers can 400.
function decodeArchiveId(s) {
  if (!s) return null;
  try { return decodeURIComponent(s) || null; }
  catch { return null; }
}

// ── /archive endpoints ───────────────────────────────────
// State machine on extractor_jobs.archive_status:
//   none → pending → processing → done  (or → failed / expired)
// Freshness window: a direct_url can only be handed off to the runner within
// ARCHIVE_FRESH_MS of its extractor_jobs.updated_at. Older links are refused
// as expired (upstream typically GCs them within minutes).
const ARCHIVE_FRESH_MS = 30 * 60 * 1000;

async function handleArchiveRequest(id, request, env) {
  const job = await env.DB.prepare(
    "SELECT id, status, direct_url, updated_at, archive_status FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!job) return json({error: "job not found"}, 404);
  if (job.status !== "success") return json({error: "job not successful yet"}, 400);
  if (!job.direct_url) return json({error: "no direct_url"}, 400);
  if (job.archive_status === "pending" || job.archive_status === "processing") {
    return json({ok: true, archive_status: job.archive_status, note: "already queued"});
  }
  if (job.archive_status === "done") {
    return json({ok: true, archive_status: "done", note: "already archived"});
  }
  // Freshness gate: refuse if updated_at is too old to trust direct_url.
  const age = Date.now() - (job.updated_at || 0);
  if (age > ARCHIVE_FRESH_MS) {
    await env.DB.prepare(
      "UPDATE extractor_jobs SET archive_status = 'expired', archive_error = ?, updated_at = ? WHERE id = ?"
    ).bind(`direct_url older than ${Math.floor(age / 60000)}min`, Date.now(), id).run();
    return json({error: "direct_url likely expired; re-run /extract first"}, 410);
  }
  await env.DB.prepare(
    "UPDATE extractor_jobs SET archive_status = 'pending', archive_error = NULL, updated_at = ? WHERE id = ?"
  ).bind(Date.now(), id).run();
  return json({ok: true, archive_status: "pending"});
}

// Runner calls this every ~2min while a long pipeline is in flight so the
// cron sweep doesn't reclaim the job as "processing but stalled". The
// owner_id must match the one we handed out at /archive-queue time — if
// a second runner has since re-claimed the reclaimed job, its owner_id is
// different and our heartbeat gets 409.
// Single-row atomic claim: pending → processing for THIS id only. Used by
// --pick so selecting one video doesn't steal every other user's pending
// jobs the way /archive-queue would (it grabs up to N oldest pending rows).
async function handleArchiveClaim(id, env) {
  const owner = crypto.randomUUID();
  const now = Date.now();
  const r = await env.DB.prepare(
    "UPDATE extractor_jobs SET archive_status = 'processing', archive_owner_id = ?, updated_at = ? " +
    "WHERE id = ? AND archive_status = 'pending'"
  ).bind(owner, now, id).run();
  if ((r.meta?.changes ?? 0) > 0) {
    return json({ok: true, owner_id: owner});
  }
  const row = await env.DB.prepare(
    "SELECT archive_status FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!row) return json({error: "job not found"}, 404);
  return json({ok: false, current_status: row.archive_status,
                note: "job not in pending; claim ignored"}, 409);
}

async function handleArchiveHeartbeat(id, request, env) {
  const {owner_id} = await request.json().catch(() => ({}));
  const r = await env.DB.prepare(
    "UPDATE extractor_jobs SET updated_at = ? " +
    "WHERE id = ? AND archive_status = 'processing' AND archive_owner_id = ?"
  ).bind(Date.now(), id, owner_id || "").run();
  const hit = (r.meta?.changes ?? 0) > 0;
  if (hit) return json({ok: true});
  const row = await env.DB.prepare(
    "SELECT archive_status FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!row) return json({error: "job not found"}, 404);
  return json({ok: false, current_status: row.archive_status,
                note: "lease lost or owner mismatch; abort pipeline"}, 409);
}

async function handleArchiveStatus(id, env) {
  const row = await env.DB.prepare(
    "SELECT archive_status, archive_key, archive_error FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!row) return json({error: "job not found"}, 404);
  return json({
    archive_status: row.archive_status || "none",
    archive_key: row.archive_key || null,
    archive_error: row.archive_error || null,
  });
}

// List fresh, unarchived success jobs — used by `archive_runner.py --pick`
// so the user can select which videos to archive from the CLI.
async function handleArchiveCandidates(url, env) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 200);
  const nowMs = Date.now();
  const freshCutoff = nowMs - ARCHIVE_FRESH_MS;
  const rows = await env.DB.prepare(`
    SELECT id, source_url, title, filename, platform, quality_actual,
           file_size, direct_url, updated_at, archive_status
    FROM extractor_jobs
    WHERE status = 'success' AND direct_url IS NOT NULL
      AND updated_at > ?
      AND (archive_status IS NULL OR archive_status IN ('none','failed'))
      AND id IN (
        SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (PARTITION BY source_url ORDER BY updated_at DESC) rn
          FROM extractor_jobs WHERE status='success' AND direct_url IS NOT NULL
        ) WHERE rn = 1
      )
    ORDER BY updated_at DESC LIMIT ?
  `).bind(freshCutoff, limit).all();
  return json({jobs: rows.results || []});
}

async function handleArchiveQueue(url, env) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "10", 10), 50);
  // Owner lease: each queue call gets a fresh UUID stamped on every claimed
  // row. Subsequent heartbeat / complete / failed must present the same
  // owner_id to succeed — prevents a stale runner from clobbering a job
  // reclaimed by cron and re-issued to a new runner.
  const owner = crypto.randomUUID();
  const now = Date.now();
  const rows = await env.DB.prepare(`
    UPDATE extractor_jobs
    SET archive_status = 'processing', archive_owner_id = ?, updated_at = ?
    WHERE id IN (
      SELECT id FROM extractor_jobs
      WHERE archive_status = 'pending' AND direct_url IS NOT NULL
      ORDER BY updated_at ASC LIMIT ?
    )
    RETURNING id, source_url, direct_url, filename, title, platform,
              quality_actual, file_size, updated_at, archive_status
  `).bind(owner, now, limit).all();
  const jobs = (rows.results || []).map(j => ({...j, owner_id: owner}));
  return json({jobs});
}

async function handleArchiveComplete(id, request, env) {
  const {media_key, owner_id} = await request.json();
  if (!media_key) return json({error: "media_key required"}, 400);
  // Atomic guard: (processing AND owner matches) → done. If cron reclaimed
  // and a new owner picked it up, THIS caller loses; we return 409 with
  // current_key so the loser can log its media_key as orphan.
  const now = Date.now();
  const r = await env.DB.prepare(
    "UPDATE extractor_jobs SET archive_status = 'done', archive_key = ?, archive_error = NULL, archive_at = ?, updated_at = ?, archive_owner_id = NULL " +
    "WHERE id = ? AND archive_status = 'processing' AND archive_owner_id = ?"
  ).bind(media_key, now, now, id, owner_id || "").run();
  const changed = (r.meta?.changes ?? 0) > 0;
  if (changed) return json({ok: true});
  const row = await env.DB.prepare(
    "SELECT archive_status, archive_key FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!row) return json({error: "job not found"}, 404);
  return json({
    ok: false,
    current_status: row.archive_status,
    current_key: row.archive_key,
    note: "lease lost / owner mismatch; complete ignored, your media_key is orphan",
  }, 409);
}

async function handleArchiveFailed(id, request, env) {
  const {reason, expired, owner_id} = await request.json();
  const status = expired ? "expired" : "failed";
  const now = Date.now();
  const r = await env.DB.prepare(
    "UPDATE extractor_jobs SET archive_status = ?, archive_error = ?, archive_key = NULL, updated_at = ?, archive_owner_id = NULL " +
    "WHERE id = ? AND archive_status = 'processing' AND archive_owner_id = ?"
  ).bind(status, String(reason || "").slice(0, 500), now, id, owner_id || "").run();
  const changed = (r.meta?.changes ?? 0) > 0;
  if (changed) return json({ok: true, archive_status: status});
  const row = await env.DB.prepare(
    "SELECT archive_status FROM extractor_jobs WHERE id = ?"
  ).bind(id).first();
  if (!row) return json({error: "job not found"}, 404);
  return json({ok: false, current_status: row.archive_status,
                note: "lease lost / owner mismatch; failure ignored"}, 409);
}

// ── /history.html ────────────────────────────────────────
// Server-rendered table: latest success-per-source_url, filename + date + link.
// Auth via ?token=<AUTH_TOKEN>.
async function handleHistoryHtml(url, env) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100", 10), 500);
  const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10), 0);
  const token = url.searchParams.get("token") || "";
  // Filter tab: 'all' (default), 'unarchived', 'archived'. Applied as WHERE
  // over the deduped row set.
  const filter = ({all:"all", unarchived:"unarchived", archived:"archived"})[url.searchParams.get("filter")] || "all";
  let filterClause = "";
  if (filter === "archived") filterClause = "AND archive_status = 'done'";
  else if (filter === "unarchived") filterClause = "AND (archive_status IS NULL OR archive_status NOT IN ('done','pending','processing'))";
  // Dedup by source_url first (keep latest success row per URL), then filter
  // by archive_status. If we filter INSIDE the CTE, we'd hide a matching
  // job whose latest row has a different archive_status than an older row.
  const q = `
    WITH dedup AS (
      SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY source_url ORDER BY updated_at DESC) rn
        FROM extractor_jobs WHERE status='success' AND direct_url IS NOT NULL
      ) WHERE rn = 1
    )
    SELECT id, source_url, title, filename, platform, quality_actual,
           file_size, direct_url, expires_at, created_at, updated_at,
           archive_status, archive_key, archive_error, archive_at
    FROM dedup
    WHERE 1=1 ${filterClause}
    ORDER BY updated_at DESC LIMIT ? OFFSET ?`;
  const rows = (await env.DB.prepare(q).bind(limit, offset).all()).results || [];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmtSize = b => !b ? "-" : b > 1e9 ? (b/1e9).toFixed(2)+" GB" : b > 1e6 ? (b/1e6).toFixed(1)+" MB" : (b/1e3).toFixed(0)+" KB";
  const fmtDate = ts => !ts ? "-" : new Date(ts).toISOString().replace("T", " ").slice(0, 19);
  const gpUrl = (k) => k ? `https://photos.google.com/lr/photo/${esc(k)}` : "";
  const archiveCell = (r) => {
    const st = r.archive_status || "none";
    if (st === "done") {
      const k = r.archive_key || "";
      const href = gpUrl(k);
      const link = href
        ? `<a class="arch done" href="${href}" target="_blank" rel="noopener" title="${esc(k)}">✅ 在 GP 查看</a>`
        : `<span class="arch done">✅ 已归档</span>`;
      const copy = k ? ` <button class="copy-btn" data-copy="${esc(k)}" title="复制 media_key">📋</button>` : "";
      return link + copy;
    }
    if (st === "pending") return `<span class="arch pending" data-poll="${esc(r.id)}">⏳ 排队中</span>`;
    if (st === "processing") return `<span class="arch pending" data-poll="${esc(r.id)}">⚙︎ 处理中</span>`;
    if (st === "expired") return `<span class="arch expired" title="${esc(r.archive_error || "")}">⏰ 已失效</span>`;
    if (st === "failed") return `<button class="arch-btn retry" data-id="${esc(r.id)}" title="${esc(r.archive_error || "")}">❌ 重试</button>`;
    return `<button class="arch-btn" data-id="${esc(r.id)}">归档 →</button>`;
  };
  const trs = rows.map(r => {
    const nameCell = r.filename || r.title || "(untitled)";
    const dlCell = r.direct_url
      ? `<a class="dl" href="${esc(r.direct_url)}" target="_blank" rel="noopener">下载 ↓</a>`
      : '<span class="dim">-</span>';
    return `<tr>
      <td class="date">${esc(fmtDate(r.updated_at))}</td>
      <td class="name" title="${esc(nameCell)}">${esc(nameCell)}</td>
      <td data-label="平台">${esc(r.platform || "-")}</td>
      <td data-label="清晰度">${esc(r.quality_actual || "-")}</td>
      <td data-label="大小">${esc(fmtSize(r.file_size))}</td>
      <td data-label="下载">${dlCell}</td>
      <td class="archcell" data-label="归档">${archiveCell(r)}</td>
      <td><a href="${esc(r.source_url)}" target="_blank" rel="noopener" class="src">原</a></td>
    </tr>`;
  }).join("");
  const nextOffset = offset + limit;
  const prevOffset = Math.max(0, offset - limit);
  const pageQs = (extra) => {
    const p = new URLSearchParams({token, limit: String(limit), filter, ...extra});
    return "?" + p.toString();
  };
  const nav = `<div class="nav">
      ${offset > 0 ? `<a href="${pageQs({offset: prevOffset})}">← 上一页</a>` : `<span class="dim">← 上一页</span>`}
      <span class="dim">offset ${offset}-${offset + rows.length}</span>
      ${rows.length === limit ? `<a href="${pageQs({offset: nextOffset})}">下一页 →</a>` : `<span class="dim">下一页 →</span>`}
    </div>`;
  const tabLink = (name, label) => {
    const p = new URLSearchParams({token, limit: String(limit), filter: name});
    const cls = name === filter ? "tab active" : "tab";
    return `<a class="${cls}" href="?${p.toString()}">${label}</a>`;
  };
  const tabs = `<div class="tabs">
      ${tabLink("all", "全部")}
      ${tabLink("unarchived", "未归档")}
      ${tabLink("archived", "已归档")}
    </div>`;
  const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Extractor History (${rows.length})</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 16px; }
  h1 { margin: 0 0 12px; font-size: 18px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #8884; vertical-align: top; }
  th { background: #8881; position: sticky; top: 0; }
  td.date { white-space: nowrap; font-family: monospace; font-size: 12px; color: #888; }
  td.name { max-width: 34vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  a.dl { text-decoration: none; padding: 3px 8px; background: #2a7; color: white; border-radius: 4px; white-space: nowrap; }
  a.dl:hover { background: #1a6; }
  a.src { color: #888; text-decoration: none; }
  a.src:hover { color: #48a; }
  .arch { font-size: 12px; }
  .arch.done { color: #2a7; }
  .arch.pending { color: #a80; }
  .arch.expired { color: #a44; }
  .arch-btn { font: inherit; padding: 3px 8px; background: #48a; color: white; border: none; border-radius: 4px; cursor: pointer; }
  .arch-btn:hover { background: #369; }
  .arch-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .arch-btn.retry { background: #a44; }
  .arch-btn.retry:hover { background: #822; }
  .copy-btn { font: inherit; padding: 2px 6px; background: transparent; border: 1px solid #8886; border-radius: 4px; cursor: pointer; margin-left: 4px; }
  .copy-btn:hover { background: #8882; }
  .copy-btn.done { background: #2a7; color: white; border-color: #2a7; }
  .nav { padding: 12px 0; }
  .nav a, .nav span { margin-right: 12px; }
  .tabs { padding: 8px 0 4px; border-bottom: 1px solid #8884; }
  .tabs .tab { display: inline-block; padding: 6px 14px; margin-right: 4px; text-decoration: none; color: inherit; border-radius: 6px 6px 0 0; }
  .tabs .tab:hover { background: #8881; }
  .tabs .tab.active { background: #48a; color: white; font-weight: 500; }
  /* Mobile: stack columns as cards. Hide the low-value ones (platform, source link) to save space. */
  @media (max-width: 700px) {
    body { padding: 8px; }
    table, thead, tbody, tr, td { display: block; }
    thead { display: none; }
    tr { border: 1px solid #8884; border-radius: 8px; padding: 8px; margin-bottom: 8px; background: #8881; }
    td { border: none; padding: 3px 0; }
    td.name { max-width: none; white-space: normal; font-weight: 500; }
    td.date { font-size: 11px; }
    td::before { content: attr(data-label) " "; color: #888; font-size: 11px; margin-right: 4px; }
    td:not([data-label])::before { content: none; }
    .tabs .tab { padding: 6px 10px; }
  }
  .dim { color: #888; }
  tr:hover td { background: #8881; }
</style></head><body>
<h1>Extractor · ${rows.length} 条 (${filter === "all" ? "全部" : filter === "archived" ? "已归档" : "未归档"})</h1>
${tabs}
${nav}
<table>
  <thead><tr>
    <th>时间</th><th>文件</th><th>平台</th><th>清晰度</th><th>大小</th><th>下载</th><th>归档</th><th>源</th>
  </tr></thead>
  <tbody>${trs || `<tr><td colspan="8" class="dim">（无记录）</td></tr>`}</tbody>
</table>
${nav}
<script>
const AUTH_TOKEN = ${JSON.stringify(token).replace(/</g, "\\u003c")};

function gpLink(key) {
  return "https://photos.google.com/lr/photo/" + encodeURIComponent(key);
}

// Render an archive cell based on the status payload from /archive-status.
function renderArchive(cell, jobId, s) {
  const st = s.archive_status || "none";
  const err = s.archive_error || "";
  const key = s.archive_key || "";
  if (st === "done") {
    const safeKey = key.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    cell.innerHTML = key
      ? '<a class="arch done" href="' + gpLink(key) + '" target="_blank" rel="noopener" title="' + safeKey + '">✅ 在 GP 查看</a>'
      : '<span class="arch done">✅ 已归档</span>';
  } else if (st === "pending" || st === "processing") {
    cell.innerHTML = '<span class="arch pending" data-poll="' + jobId + '">' +
      (st === "pending" ? "⏳ 排队中" : "⚙︎ 处理中") + '</span>';
    schedulePoll(cell, jobId);
  } else if (st === "expired") {
    const safeErr = err.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    cell.innerHTML = '<span class="arch expired" title="' + safeErr + '">⏰ 已失效</span>';
  } else if (st === "failed") {
    const safeErr = err.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    cell.innerHTML = '<button class="arch-btn retry" data-id="' + jobId + '" title="' + safeErr + '">❌ 重试</button>';
    wireArchiveBtn(cell.querySelector(".arch-btn"));
  } else {
    cell.innerHTML = '<button class="arch-btn" data-id="' + jobId + '">归档 →</button>';
    wireArchiveBtn(cell.querySelector(".arch-btn"));
  }
}

const _polling = new Set();
function schedulePoll(cell, jobId) {
  if (_polling.has(jobId)) return;
  _polling.add(jobId);
  const tick = async () => {
    try {
      const r = await fetch("/archive-status/" + encodeURIComponent(jobId) + "?token=" + encodeURIComponent(AUTH_TOKEN));
      const j = await r.json();
      if (!r.ok) { _polling.delete(jobId); return; }
      const st = j.archive_status || "none";
      if (st === "done" || st === "failed" || st === "expired") {
        _polling.delete(jobId);
        renderArchive(cell, jobId, j);
      } else {
        setTimeout(tick, 4000);
      }
    } catch (_) {
      setTimeout(tick, 8000);
    }
  };
  setTimeout(tick, 3000);
}

function wireArchiveBtn(btn) {
  if (!btn || btn._wired) return;
  btn._wired = true;
  btn.addEventListener("click", async () => {
    const id = btn.dataset.id;
    const cell = btn.closest(".archcell");
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = "提交中...";
    try {
      const r = await fetch("/archive/" + encodeURIComponent(id), {
        method: "POST",
        headers: { "X-Auth": AUTH_TOKEN },
      });
      const j = await r.json();
      if (r.ok) {
        renderArchive(cell, id, j);
      } else if (r.status === 410) {
        cell.innerHTML = '<span class="arch expired">⏰ 已失效</span>';
      } else {
        btn.disabled = false;
        btn.textContent = orig;
        alert("失败: " + (j.error || r.status));
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = orig;
      alert("请求失败: " + e.message);
    }
  });
}

// Wire everything on load.
document.querySelectorAll(".arch-btn").forEach(wireArchiveBtn);
document.querySelectorAll('[data-poll]').forEach(el => {
  schedulePoll(el.closest('.archcell'), el.dataset.poll);
});
// Copy media_key to clipboard.
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".copy-btn");
  if (!btn) return;
  const val = btn.dataset.copy;
  if (!val) return;
  (navigator.clipboard?.writeText(val) || Promise.reject())
    .catch(() => {
      const ta = document.createElement("textarea");
      ta.value = val; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (_) {}
      document.body.removeChild(ta);
    })
    .finally(() => {
      const orig = btn.textContent;
      btn.textContent = "✓";
      btn.classList.add("done");
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("done"); }, 1200);
    });
});
</script>
</body></html>`;
  return new Response(html, {headers: {"content-type": "text/html; charset=utf-8"}});
}

// ── /jobs ────────────────────────────────────────────────
async function handleJobs(url, env) {
  const status = url.searchParams.get("status");
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 500);
  const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10), 0);
  const distinct = url.searchParams.get("distinct") === "1";
  const cols = `id, status, source_url, service, title, filename, platform,
                quality_actual, file_size, direct_url, expires_at, error,
                created_at, updated_at`;
  let q;
  const args = [];
  if (distinct) {
    // Keep only the latest row per source_url. Filtering happens before
    // LIMIT so pagination stays consistent.
    q = `SELECT ${cols} FROM extractor_jobs
         WHERE id IN (
           SELECT id FROM (
             SELECT id, source_url, updated_at,
                    ROW_NUMBER() OVER (PARTITION BY source_url ORDER BY updated_at DESC) rn
             FROM extractor_jobs
             ${status ? "WHERE status = ?" : ""}
           ) WHERE rn = 1
         )`;
    if (status) args.push(status);
    q += " ORDER BY updated_at DESC LIMIT ? OFFSET ?";
    args.push(limit, offset);
  } else {
    q = `SELECT ${cols} FROM extractor_jobs`;
    if (status) { q += " WHERE status = ?"; args.push(status); }
    q += " ORDER BY updated_at DESC LIMIT ? OFFSET ?";
    args.push(limit, offset);
  }
  const rows = await env.DB.prepare(q).bind(...args).all();
  return json({jobs: rows.results || []});
}

// ── selectService (balancer) ─────────────────────────────
function safeJson(s, def) { try { return JSON.parse(s); } catch (_) { return def; } }

function cooldownActive(svc) {
  const consec = svc.consecutive_429 || 0;
  if (consec === 0) return false;
  // Exponential backoff. Short first stage so a single 429 doesn't block the
  // service for minutes; longer stages guard against persistent upstream limits.
  // Stages: 1m, 5m, 15m, 1h.
  const stages = [1, 5, 15, 60];
  const idx = Math.min(consec - 1, stages.length - 1);
  const cooldownMs = stages[idx] * 60 * 1000;
  return Date.now() - (svc.last_429_at || 0) < cooldownMs;
}

// Kept in lockstep with streamlink plugin `vthreads.py` @pluginmatcher whitelist.
// Anything not in this map -> "unknown" -> selectService will reject unless the
// service has "*". vthreads deliberately does NOT have "*" to avoid slamming the
// cloud with 20-30s probes for every random URL.
function detectPlatform(url) {
  const u = url.toLowerCase();
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
  if (u.includes("bilibili.com") || u.includes("b23.tv")) return "bilibili";
  if (u.includes("douyin.com") || u.includes("iesdouyin.com")) return "douyin";
  if (u.includes("tiktok.com")) return "tiktok";
  if (u.includes("twitter.com") || u.includes("x.com")) return "x";
  if (u.includes("instagram.com")) return "instagram";
  if (u.includes("facebook.com") || u.includes("fb.watch")) return "facebook";
  if (u.includes("reddit.com") || u.includes("redd.it")) return "reddit";
  if (u.includes("vimeo.com")) return "vimeo";
  if (u.includes("dailymotion.com") || u.includes("dai.ly")) return "dailymotion";
  if (u.includes("xiaohongshu.com") || u.includes("xhslink.com")) return "xiaohongshu";
  if (u.includes("weibo.com") || u.includes("weibo.cn")) return "weibo";
  if (u.includes("kuaishou.com")) return "kuaishou";
  if (u.includes("twitch.tv")) return "twitch";
  if (u.includes("soundcloud.com")) return "soundcloud";
  if (u.includes("ok.ru")) return "okru";
  if (u.includes("rumble.com")) return "rumble";
  if (u.includes("odysee.com")) return "odysee";
  if (u.includes("bitchute.com")) return "bitchute";
  if (u.includes("streamable.com")) return "streamable";
  if (u.includes("v.qq.com")) return "qq";
  if (u.includes("iqiyi.com")) return "iqiyi";
  if (u.includes("mgtv.com")) return "mgtv";
  if (u.includes("youku.com")) return "youku";
  if (u.includes("ixigua.com")) return "ixigua";
  if (u.includes("ted.com")) return "ted";
  if (u.includes("archive.org")) return "archive";
  if (u.includes("pornhub.com")) return "pornhub";
  if (u.includes("xvideos.com")) return "xvideos";
  if (u.includes("xhamster.com")) return "xhamster";
  if (u.includes("redtube.com")) return "redtube";
  if (u.includes("youporn.com")) return "youporn";
  if (u.includes("spankbang.com")) return "spankbang";
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
// On RateLimitError (429 / 1015 / daily-quota), immediately retry with a
// fresh proxy IP + spoofed headers. Pool has hundreds of IPs and X-Forwarded-For
// spoofing gives 30 quota per (IP, fake-XFF), so a rate-limit here is a hint to
// pick another combination — not a reason to sleep or bubble up.
//
// The service-level cooldown is intentionally NOT tripped by these retries
// (see nudgeJob's RateLimitError branch, which now only marks service after
// maxAttempts is fully exhausted). This means a burnt-out single IP no longer
// blocks the whole service.
async function callUpstreamWithRetry(service, action, params, env, maxAttempts = 10) {
  const backoff = [200, 400, 800, 1200, 1600];
  let lastErr;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await callUpstream(service, action, params, env);
    } catch (e) {
      lastErr = e;
      if (!(e instanceof RateLimitError)) throw e;
      if (i === maxAttempts - 1) break;
      // Short sleep so we don't slam the same colo — pickProxyIp on next attempt
      // is randomized, so a different IP will most likely be chosen.
      await sleep(backoff[Math.min(i, backoff.length - 1)]);
    }
  }
  throw lastErr;
}

async function callUpstream(service, action, params, env) {
  const headers = safeJson(service.required_headers, {});
  if (service.api_type === "vthreads") return callVthreads(service, action, params, headers, env);
  if (service.api_type === "savenow")  return callSavenow(service, action, params, headers, env);
  if (service.api_type === "cobalt")   return callCobalt(service, action, params, headers);
  if (service.api_type === "metube")   return callMetube(service, action, params, headers);
  throw new Error(`unsupported api_type: ${service.api_type}`);
}

// Removed VTHREADS_FALLBACK_IP: IPv6 resolveOverride support unverified; untracked
// fallback bypasses cooldown accounting. Empty pool → direct fetch (may 1015).

// Random-ish public-space IPv4 for X-Forwarded-For spoofing. Confirmed by
// /probe-header-bypass: vthreads counts 30/day per X-Forwarded-For value, so a
// fresh random per call gives us effectively unlimited quota per proxy IP.
// Uses reserved-doc range 203.0.113.0/24 + a shuffled octet so different jobs
// don't collide with the same fake IP within one CF colo minute.
function randomFakeIp() {
  const o1 = 100 + Math.floor(Math.random() * 120);   // 100-219
  const o2 = Math.floor(Math.random() * 256);
  const o3 = Math.floor(Math.random() * 256);
  const o4 = 1 + Math.floor(Math.random() * 253);
  return `${o1}.${o2}.${o3}.${o4}`;
}

async function callVthreads(service, action, params, headers, env) {
  const proxyIp = env ? await pickProxyIp(env) : null;
  const fakeIp = randomFakeIp();
  const spoofHeaders = {
    ...headers,
    "X-Forwarded-For": fakeIp,
    "X-Real-IP": fakeIp,
    "CF-Connecting-IP": fakeIp,
    "True-Client-IP": fakeIp,
    "X-Client-IP": fakeIp,
  };
  const fetchOpts = (opts) => ({
    ...opts,
    headers: {...(opts?.headers || {}), ...spoofHeaders},
    cf: proxyIp ? {resolveOverride: proxyIp} : undefined,
  });

  // Reason: 'cf1015' → short cooldown (CF edge rate-limit), 'daily' → 24h (per-IP
  // vthreads daily-quota exhausted). null → not rate-limited.
  const markResult = async (rlReason, err) => {
    if (!env || !proxyIp) return;
    if (rlReason === 'daily') await markProxyDaily(env, proxyIp);
    else if (rlReason) await markProxyCooldown(env, proxyIp);
    else if (err) await markProxyDead(env, proxyIp, err);
    else await markProxyOk(env, proxyIp);
  };

  // Classify 429 body: "今日使用次数已达上限" → daily quota; anything else → cf1015-ish.
  const classifyRateLimit = (body) => {
    if (body && body.includes("今日使用次数已达上限")) return 'daily';
    return 'cf1015';
  };

  // For submit: HEAD first to detect direct-stream inline mp4
  if (action === "submit") {
    const u = `${service.base_url}${params.rel_url}`;
    let head;
    try {
      head = await fetch(u, fetchOpts({method: "HEAD", headers}));
    } catch (e) { await markResult(null, e); throw e; }
    if (head.status === 429) { await markResult('cf1015'); throw new RateLimitError(`vthreads 429 on submit`); }
    const ct = (head.headers.get("content-type") || "").toLowerCase();
    if (ct.startsWith("video/") || ct.startsWith("audio/")) {
      await markResult(null, null);
      return {
        _direct_stream: true,
        download_url: u,
        file_size: parseInt(head.headers.get("content-length") || "0", 10) || null,
      };
    }
    let r, text;
    try {
      r = await fetch(u, fetchOpts({headers}));
      text = await r.text();
    } catch (e) { await markResult(null, e); throw e; }
    if (r.status === 429) { await markResult(classifyRateLimit(text)); throw new RateLimitError(`vthreads 429 on submit: ${text.slice(0,80)}`); }
    if (text.includes("error code: 1015")) { await markResult('cf1015'); throw new RateLimitError(`vthreads CF 1015 on submit`); }
    if (!r.ok) { const err = new Error(`vthreads submit HTTP ${r.status}: ${text.slice(0, 200)}`); await markResult(null, err); throw err; }
    if (!text.trimStart().startsWith("{")) { const err = new Error(`vthreads submit non-JSON: ${text.slice(0, 200)}`); await markResult(null, err); throw err; }
    await markResult(null, null);
    return JSON.parse(text);
  }

  // extract / status
  const doFetch = async (u) => {
    let r, text;
    try {
      r = await fetch(u, fetchOpts({headers}));
      text = await r.text();
    } catch (e) { await markResult(null, e); throw e; }
    if (r.status === 429) { await markResult(classifyRateLimit(text)); throw new RateLimitError(`vthreads 429 on ${action}: ${text.slice(0,80)}`); }
    if (text.includes("error code: 1015") || text.includes("Too Many Requests")) {
      await markResult('cf1015');
      throw new RateLimitError(`vthreads CF 1015 rate limit on ${action}`);
    }
    if (!r.ok) { const err = new Error(`vthreads ${action} HTTP ${r.status}: ${text.slice(0, 200)}`); await markResult(null, err); throw err; }
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("json") && !text.trimStart().startsWith("{")) {
      const err = new Error(`vthreads ${action} non-JSON: ${text.slice(0, 200)}`);
      await markResult(null, err);
      throw err;
    }
    try {
      const parsed = JSON.parse(text);
      await markResult(null, null);
      return parsed;
    } catch (e) {
      const err = new Error(`vthreads ${action} bad JSON: ${text.slice(0, 200)}`);
      await markResult(null, err);
      throw err;
    }
  };

  if (action === "extract") return await doFetch(`${service.base_url}/api/extract?url=${encodeURIComponent(params.url)}&lang=zh`);
  if (action === "status") return await doFetch(`${service.base_url}/api/check_status/${params.task_id}`);
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

// ── savenow.to (video-download-api.com) adapter ─────────────
//
// Real API surface:
//   GET /api/v2/download?url=X&format=F&apikey=K  → {success, id, ...}
//   GET /api/progress?id=X                        → {progress:0-1000, download_url, text}
//
// The rest of the worker was written against vthreads' two-step
// extract/submit/status flow, so this adapter emulates that shape:
//   extract(url)              → synth medias list (no upstream call) [1]
//   submit(rel_url|format)    → real POST /api/v2/download → returns task_id
//   status(task_id)           → poll /api/progress → maps to vthreads status enum
//
// [1] savenow charges per completed request, so we don't burn one just to
// enumerate qualities. The medias list is built from the standard format
// menu; pickQuality() then chooses a matching entry which encodes the
// format token we submit for real.
const SAVENOW_QUALITIES = [
  { quality: "4k",   format_key: "mp44k",  key_url: "?f=mp44k" },
  { quality: "1440", format_key: "1440",   key_url: "?f=1440"  },
  { quality: "1080", format_key: "1080",   key_url: "?f=1080"  },
  { quality: "720",  format_key: "720",    key_url: "?f=720"   },
  { quality: "480",  format_key: "480",    key_url: "?f=480"   },
  { quality: "360",  format_key: "360",    key_url: "?f=360"   },
  { quality: "144",  format_key: "144",    key_url: "?f=144"   },
  { quality: "mp3",  format_key: "mp3",    key_url: "?f=mp3"   },
];

async function callSavenow(service, action, params, headers, env) {
  const apikey = await getSavenowApiKey(env);
  if (!apikey) {
    throw new Error("savenow: no API key available (empty savenow_keys table and auto-register disabled)");
  }

  if (action === "extract") {
    // No upstream call — synth a fake extract response so nudgeJob's vthreads
    // code path finds a medias list and picks a quality. The chosen media's
    // .url will be echoed to our submit(), which is where we hit savenow.
    return {
      success: true,
      data: {
        title: "",
        platform: "youtube",
        medias: SAVENOW_QUALITIES.map(q => ({
          quality: q.quality,
          url: q.key_url,       // opaque to caller — we parse ?f= back out in submit()
          format: "mp4",
        })),
      },
    };
  }

  if (action === "submit") {
    // rel_url is one of our synth entries — extract the format token.
    const rel = params.rel_url || "";
    const m = /\?f=([\w-]+)/.exec(rel);
    const fmt = m ? m[1] : "1080";
    const submitUrl = `${service.base_url}/api/v2/download`
      + `?url=${encodeURIComponent(params.source_url || params.url || "")}`
      + `&format=${encodeURIComponent(fmt)}`
      + `&apikey=${encodeURIComponent(apikey)}`
      + `&add_info=1&allow_extended_duration=1`;
    const r = await fetch(submitUrl, {headers: {"User-Agent": headers["User-Agent"] || "Mozilla/5.0"}});
    const text = await r.text();
    if (r.status === 429) throw new RateLimitError(`savenow submit 429`);
    if (!r.ok) throw new Error(`savenow submit HTTP ${r.status}: ${text.slice(0, 200)}`);
    let j; try { j = JSON.parse(text); } catch { throw new Error(`savenow submit non-JSON: ${text.slice(0, 200)}`); }
    if (!j.success) {
      const msg = (j.text || j.error || "").toString().toLowerCase();
      if (msg.includes("insufficient") || msg.includes("wallet") || msg.includes("balance") || msg.includes("credit")) {
        await retireSavenowKey(env, apikey);
        throw new Error(`savenow key exhausted: ${text.slice(0, 200)}`);
      }
      throw new Error(`savenow submit not success: ${text.slice(0, 200)}`);
    }
    // Adapt to vthreads shape — nudgeJob reads .task_id + writes it to job.ext_task_id.
    return {
      task_id: j.id,
      _savenow_progress_url: j.progress_url || null,
      _savenow_apikey: apikey,
    };
  }

  if (action === "status") {
    const id = params.task_id;
    const r = await fetch(`${service.base_url}/api/progress?id=${encodeURIComponent(id)}`);
    if (r.status === 429) throw new RateLimitError(`savenow status 429`);
    const text = await r.text();
    if (!r.ok) throw new Error(`savenow status HTTP ${r.status}: ${text.slice(0, 200)}`);
    let j; try { j = JSON.parse(text); } catch { throw new Error(`savenow status non-JSON: ${text.slice(0, 200)}`); }
    // savenow encodes progress on a 0..1000 scale; 1000 == 100%.
    const progressRaw = Number(j.progress || 0);
    const progressPct = Math.min(100, Math.round(progressRaw / 10));
    const dl = j.download_url;
    const text_l = (j.text || "").toString().toLowerCase();
    let status = "POLLING";
    if (dl) status = "SUCCESS";
    else if (text_l === "failed" || text_l.startsWith("error")) status = "FAILED";
    return {
      status,
      progress: progressPct,
      download_url: dl || undefined,
      file_size: j.file_size || undefined,
      supports_range: true,   // savenow's storage URLs support Range
    };
  }

  throw new Error(`unknown savenow action: ${action}`);
}

// D1-persisted savenow API keys. Table schema (see migration below):
//   savenow_keys(api_key TEXT PRIMARY KEY, email TEXT, password TEXT,
//     balance_micro INTEGER, retired INTEGER DEFAULT 0, created_at INTEGER)
async function getSavenowApiKey(env) {
  try {
    const row = await env.DB.prepare(
      "SELECT api_key FROM savenow_keys WHERE retired=0 AND balance_micro > 0 ORDER BY balance_micro DESC LIMIT 1",
    ).first();
    if (row && row.api_key) return row.api_key;
  } catch (e) {
    console.log("savenow: getSavenowApiKey read fail:", e.message);
  }
  // Fall back to a registered-at-worker-start env value if operator wanted to pin one.
  if (env.SAVENOW_API_KEY) return env.SAVENOW_API_KEY;
  // Last resort: auto-register a new account inside the worker.
  const acc = await registerSavenowAccount(env);
  return acc ? acc.api_key : null;
}

async function retireSavenowKey(env, apikey) {
  try {
    await env.DB.prepare("UPDATE savenow_keys SET retired=1, balance_micro=0 WHERE api_key=?")
      .bind(apikey).run();
  } catch (e) { console.log("savenow retire fail:", e.message); }
}

async function registerSavenowAccount(env) {
  const origin = env.SAVENOW_REGISTER_ORIGIN || "https://video-download-api.com";
  const ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36";
  // Step 1: GET /register to grab CSRF + cookies. fetch() alone doesn't handle
  // cookies transparently, so we forward them by hand.
  const r1 = await fetch(`${origin}/register`, {headers: {"User-Agent": ua}});
  const html1 = await r1.text();
  const csrf = /<meta name="csrf-token" content="([^"]+)"/.exec(html1)?.[1];
  const cookies = collectCookies(r1);
  if (!csrf) return null;

  const rand = (n) => Array.from({length: n}, () => "abcdefghijklmnopqrstuvwxyz"[Math.floor(Math.random() * 26)]).join("");
  const email = rand(10) + "@outlook.com";
  const name = rand(8).replace(/^./, c => c.toUpperCase());
  const password = crypto.randomUUID();
  const body = new URLSearchParams({
    _token: csrf, name, email, password, password_confirmation: password,
  }).toString();

  const r2 = await fetch(`${origin}/register`, {
    method: "POST",
    headers: {
      "User-Agent": ua,
      "Content-Type": "application/x-www-form-urlencoded",
      "Referer": `${origin}/register`,
      "X-XSRF-TOKEN": csrf,
      "Cookie": cookies,
    },
    body,
    redirect: "follow",
  });
  const html2 = await r2.text();
  if (!r2.url.endsWith("/dashboard")) return null;
  const pageMatch = /data-page="([^"]+)"/.exec(html2);
  if (!pageMatch) return null;
  let page;
  try { page = JSON.parse(pageMatch[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&#039;/g, "'")); }
  catch { return null; }
  const block = page?.props?.apiKey || {};
  const apiKey = block.key;
  const balanceMicro = block.balanceMicro || 0;
  if (!apiKey) return null;
  const acc = {api_key: apiKey, email, password, balance_micro: balanceMicro, retired: 0, created_at: Date.now()};
  try {
    await env.DB.prepare(
      "INSERT OR REPLACE INTO savenow_keys (api_key, email, password, balance_micro, retired, created_at) VALUES (?, ?, ?, ?, 0, ?)",
    ).bind(acc.api_key, acc.email, acc.password, acc.balance_micro, acc.created_at).run();
  } catch (e) { console.log("savenow register save fail:", e.message); }
  return acc;
}

function collectCookies(res) {
  // fetch()'s Response.headers.getSetCookie() returns the individual
  // Set-Cookie lines. Turn them into a "name=value; name=value" string
  // suitable for the follow-up Cookie header.
  try {
    const raw = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
    return raw.map(line => line.split(";")[0]).join("; ");
  } catch (_) { return ""; }
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
    // Respect upstream cooldown — skip nudge, next cron tick tries again once cooldown expires.
    if (cooldownActive(service)) {
      job.message = "换 IP 重试中,稍等...";
      await saveJob(env, job);
      ctx.waitUntil(notifyTgIfNeeded(job, env));
      return;
    }

    if (job.status === "pending") {
      // extract + submit in one go.
      const ext = await callUpstreamWithRetry(service, "extract", {url: job.source_url}, env);

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
        // Ensure D1 non-undefined for typed columns (D1 rejects undefined bindings)
        job.format = job.format || "mp4";
        job.quality_actual = job.quality_actual || "unknown";
        job.file_size = job.file_size || null;
        job.duration_s = job.duration_s || null;
        job.message = job.message || "direct (cobalt)";
        await saveJob(env, job);
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
        ctx.waitUntil(notifyTgIfNeeded(job, env));
        return;
      }

      // vthreads path
      if (!ext.success) throw new Error(`upstream extract failed: ${JSON.stringify(ext).slice(0,200)}`);
      const medias = ext.data?.medias || [];
      if (!medias.length) throw new Error("upstream returned no medias");
      const media = pickQuality(medias, job.quality);
      if (!media) throw new Error("no matching quality");

      const sub = await callUpstreamWithRetry(service, "submit", {rel_url: media.url}, env);

      // vthreads direct-stream: file served inline on /api/download_merge (no task).
      if (sub._direct_stream) {
        job.title = ext.data.title || "";
        job.platform = ext.data.platform || detectPlatform(job.source_url);
        job.filename = sanitize(job.title || job.source_url) + guessExt(job.platform);
        job.format = "mp4";
        job.quality_actual = media.quality;
        job.direct_url = sub.download_url;
        job.file_size = sub.file_size;
        job.required_headers = service.required_headers;
        job.expires_at = Date.now() + (service.direct_url_ttl_s || 3600) * 1000;
        job.supports_range = 1;
        job.status = "success";
        job.message = "direct stream (no async task)";
        await saveJob(env, job);
        if (job.webhook) ctx.waitUntil(fireWebhook(job));
        ctx.waitUntil(notifyTgIfNeeded(job, env));
        return;
      }

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
      ctx.waitUntil(notifyTgIfNeeded(job, env));
      return;
    }

    if (job.status === "polling") {
      // Poll in a tight loop within one invocation instead of returning after a
      // single check and waiting for the next /status ping or cron tick. Bounded
      // by lease TTL (60s) and a hard iteration cap. Each poll ~500ms upstream.
      const POLL_INTERVAL_MS = 2000;
      const POLL_MAX_ITER = 20;
      const START = Date.now();
      const LEASE_MARGIN_MS = 45000;
      for (let i = 0; i < POLL_MAX_ITER; i++) {
        let st;
        try {
          st = await callUpstreamWithRetry(service, "status", {task_id: job.ext_task_id}, env);
        } catch (e) {
          // vthreads garbage-collects async tasks aggressively (~5min). If our
          // ext_task_id 404s with "任务不存在或已过期", restart from extract by
          // reverting to pending; the caller (or cron) will re-submit and get a
          // fresh task_id. Preserves user's request without a hard failure.
          const msg = String(e && e.message || e);
          if (msg.includes("HTTP 404") && msg.includes("任务不存在或已过期")) {
            job.status = "pending";
            job.ext_task_id = null;
            job.ext_status = null;
            job.ext_progress = 0;
            job.message = "上游任务已过期,重新提交";
            job.retry_count = (job.retry_count || 0) + 1;
            if (job.retry_count > 3) {
              job.status = "failed";
              job.error = "upstream task expired 3+ times";
            }
            await saveJob(env, job);
            return;
          }
          throw e;
        }
        job.ext_status = st.status;
        job.ext_progress = st.progress || 0;
        job.message = `upstream ${st.status}${st.progress != null ? " " + st.progress + "%" : ""}`;
        if (st.status === "SUCCESS") {
          let du = st.download_url || "";
          if (du && !du.startsWith("http")) du = service.base_url + du;
          job.direct_url = du;
          job.file_size = st.file_size || null;
          job.supports_range = st.supports_range === false ? 0 : 1;
          job.required_headers = service.required_headers;
          job.expires_at = Date.now() + (service.direct_url_ttl_s || 3600) * 1000;
          job.status = "success";
          await saveJob(env, job);
          if (job.webhook) ctx.waitUntil(fireWebhook(job));
          ctx.waitUntil(notifyTgIfNeeded(job, env));
          return;
        }
        if (st.status === "FAILED") {
          job.status = "failed";
          job.error = "upstream reported FAILED";
          await saveJob(env, job);
          if (job.webhook) ctx.waitUntil(fireWebhook(job));
          ctx.waitUntil(notifyTgIfNeeded(job, env));
          return;
        }
        // Persist progress each iteration so /status GET sees live updates.
        await saveJob(env, job);
        // Bail before lease expires so a follow-up nudge can acquire the lock.
        if (Date.now() - START > LEASE_MARGIN_MS) break;
        await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
      }
      ctx.waitUntil(notifyTgIfNeeded(job, env));
      return;
    }
    // terminal: nothing to do
  } catch (err) {
    if (job) {
      // Rate limit: use a separate soft-cap counter so job doesn't spin forever
      // if upstream is permanently rate-limiting. Post-spoof, callUpstreamWithRetry
      // already burns 10 IPs before throwing — so hitting this branch means real
      // service-wide trouble. Cap at 5 (=50 total IP attempts) before giving up.
      if (err instanceof RateLimitError) {
        await markService429(env, job.service);
        const key = `_rl_${jobId}`;
        const count = (_rateLimitCount.get(key) || 0) + 1;
        _rateLimitCount.set(key, count);
        if (count >= 5) {
          job.status = "failed";
          job.error = "upstream persistently rate-limited (5×10 attempts)";
          if (job.webhook) ctx.waitUntil(fireWebhook(job));
          _rateLimitCount.delete(key);
        } else {
          job.error = null;
          job.message = `换 IP 重试中,稍等...`;
        }
        await saveJob(env, job);
        ctx.waitUntil(notifyTgIfNeeded(job, env));
      } else {
        // Real error → count it
        job.retry_count = (job.retry_count || 0) + 1;
        job.error = String(err).slice(0, 500);
        if (job.retry_count >= 3) {
          job.status = "failed";
          if (job.webhook) ctx.waitUntil(fireWebhook(job));
        }
        await saveJob(env, job);
        ctx.waitUntil(notifyTgIfNeeded(job, env));
      }
    }
  } finally {
    await env.DB.prepare(
      "UPDATE extractor_jobs SET owner_id=NULL, owner_expires=NULL WHERE id=?"
    ).bind(jobId).run();
  }
}

async function saveJob(env, job) {
  // Clear rate-limit counter on terminal states
  if (job.status === "success" || job.status === "failed") {
    _rateLimitCount.delete(`_rl_${job.id}`);
  }
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
  // Prefer format=merge (server-side ffmpeg output, real mp4) over format=mp4 (which
  // vthreads uses for /api/proxy — thin YouTube signed-URL passthrough that CF can't fetch).
  const preferMerge = (list) => {
    const merge = list.filter(x => (x.m.format || "").toLowerCase() === "merge");
    return merge.length ? merge : list;
  };
  const scored = medias.map(m => ({m, s: qualityScore(m)}));

  if (pref === "audio_only") {
    return medias.find(m => m.quality?.toLowerCase().includes("audio")) || null;
  }
  if (pref === "best") {
    const pool = preferMerge(scored);
    pool.sort((a, b) => b.s - a.s);
    return pool[0]?.m;
  }
  if (pref === "smallest") {
    const pool = preferMerge(scored);
    const withSize = pool.filter(x => x.m.size);
    if (withSize.length) {
      withSize.sort((a, b) => parseSize(a.m.size) - parseSize(b.m.size));
      return withSize[0].m;
    }
    pool.sort((a, b) => a.s - b.s);
    return pool[0]?.m;
  }
  // Exact/partial quality label match, still prefer merge
  const pool = preferMerge(scored);
  const exact = pool.find(x => (x.m.quality || "").toLowerCase().includes(pref));
  if (exact) return exact.m;
  const target = qualityNum(pref);
  const below = pool.filter(x => x.s <= target).sort((a, b) => b.s - a.s);
  if (below.length) return below[0].m;
  pool.sort((a, b) => b.s - a.s);
  return pool[0]?.m;
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

// ── Proxy IP pool ─────────────────────────────────────────
// Rotate outbound to vthreads via cf.resolveOverride using community-provided
// proxy IPs. Bypasses per-source-IP rate limits (Cloudflare 1015).
//
// Source: https://ipdb.api.030101.xyz/?type=bestproxy (community-optimized).
// Each IP tracked in D1 proxy_ips: untested → healthy → cooldown (5m on 1015) or dead (3+ net errs).
// Use type=proxy (larger raw list, 10-min refresh) rather than bestproxy (curated, half-hour). More candidates → more likely to find IPs that actually reverse-proxy vthreads (not just generic CF IPs).
const PROXY_IP_SOURCE = "https://ipdb.api.030101.xyz/?type=proxy";
const PROXY_REFRESH_MS = 30 * 60 * 1000;   // refresh source every 30 min
const PROXY_COOLDOWN_MS = 30 * 1000;   // 429/1015 cooldown per IP (short — vthreads rate-limits by URL, not IP, so IPs recover fast)
const PROXY_DEAD_THRESHOLD = 3;

async function pickProxyIp(env) {
  // Post-spoof-header: X-Forwarded-For randomization already sidesteps
  // vthreads' 30/day per-IP counter. The pool now only helps with CF-edge
  // 1015 (per-source-IP rate-limit on the CF endpoint). Any usable IP is
  // fine — just pick one that's not currently on cooldown. Skip status
  // filtering: dead/untested/healthy all work equally after spoof.
  const now = Date.now();
  const row = await env.DB.prepare(
    "SELECT ip FROM proxy_ips WHERE status != 'dead' AND (cooldown_until IS NULL OR cooldown_until = 0 OR cooldown_until < ?) ORDER BY RANDOM() LIMIT 1"
  ).bind(now).first();
  if (row) return row.ip;
  // Everything cooling. Fall back to any non-dead IP.
  const any = await env.DB.prepare(
    "SELECT ip FROM proxy_ips WHERE status != 'dead' ORDER BY RANDOM() LIMIT 1"
  ).first();
  return any?.ip || null;
}

async function markProxyOk(env, ip) {
  if (!ip) return;
  const now = Date.now();
  await env.DB.prepare(
    "UPDATE proxy_ips SET status='healthy', last_success_at=?, success_count=success_count+1, cooldown_until=0, dead_count=0, updated_at=? WHERE ip=?"
  ).bind(now, now, ip).run();
}

async function markProxyCooldown(env, ip) {
  if (!ip) return;
  const now = Date.now();
  await env.DB.prepare(
    "UPDATE proxy_ips SET status='cooldown', cooldown_until=?, fail_count=fail_count+1, updated_at=? WHERE ip=?"
  ).bind(now + PROXY_COOLDOWN_MS, now, ip).run();
}

// vthreads per-IP daily quota (30/day). Cool down until tomorrow (24h from now
// is a coarse proxy — vthreads resets on some clock, not per-IP timer, but 24h
// is safe overshoot). Preserves success_count so the IP stays a known-good.
async function markProxyDaily(env, ip) {
  if (!ip) return;
  const now = Date.now();
  await env.DB.prepare(
    "UPDATE proxy_ips SET status='cooldown', cooldown_until=?, fail_count=fail_count+1, updated_at=? WHERE ip=?"
  ).bind(now + 24 * 60 * 60 * 1000, now, ip).run();
}

async function markProxyDead(env, ip, err) {
  if (!ip) return;
  const now = Date.now();
  await env.DB.prepare(`
    UPDATE proxy_ips SET
      dead_count = dead_count + 1,
      last_error = ?,
      updated_at = ?,
      status = CASE WHEN dead_count + 1 >= ${PROXY_DEAD_THRESHOLD} THEN 'dead' ELSE status END
    WHERE ip=?
  `).bind(String(err).slice(0, 200), now, ip).run();
}

// Fetch ipdb through: direct → each healthy proxy IP via resolveOverride.
// Returns {ok, text, via, attempts:[{via,status,ct,len}]}. Worker-direct fetch
// is Turnstile-blocked; proxy IPs may or may not carry ipdb's cert SANs. Try all.
async function fetchIpdbThroughPool(env, {sampleIps = 5} = {}) {
  const ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36";
  const headers = {"User-Agent": ua};
  const attempts = [];
  const tryFetch = async (label, opts) => {
    try {
      const r = await fetch(PROXY_IP_SOURCE, {headers, ...opts});
      const ct = r.headers.get("content-type") || "";
      const text = await r.text();
      // Turnstile challenge is HTML (~5KB+); real payload is IPs (plain text) or JSON.
      // Accept only when body actually contains IP-shaped lines.
      const looksLikeIps = /^\s*(\d+\.\d+\.\d+\.\d+|\[[0-9a-fA-F:]+\])/m.test(text);
      attempts.push({via: label, status: r.status, ct: ct.slice(0, 40), len: text.length, ok: r.ok && looksLikeIps});
      if (r.ok && looksLikeIps) return {ok: true, text};
    } catch (e) {
      attempts.push({via: label, error: String(e).slice(0, 120)});
    }
    return null;
  };

  // 1) Direct (fastest, most likely to fail due to Turnstile)
  let res = await tryFetch("direct", {});
  if (res) return {...res, via: "direct", attempts};

  // 2) Any usable proxy IP (healthy first, then untested — cooldown/dead excluded).
  // Refresh path can't rely solely on 'healthy' since the pool starts all-untested.
  const rows = await env.DB.prepare(
    "SELECT ip FROM proxy_ips WHERE status IN ('healthy','untested') ORDER BY (status='healthy') DESC, RANDOM() LIMIT ?"
  ).bind(sampleIps).all();
  for (const {ip} of rows.results || []) {
    res = await tryFetch(`ip:${ip}`, {cf: {resolveOverride: ip}});
    if (res) return {...res, via: `ip:${ip}`, attempts};
  }
  return {ok: false, attempts};
}

// Debug variant that returns detail on what happened
async function refreshProxyIpsDebug(env) {
  const now = Date.now();
  const fetched = await fetchIpdbThroughPool(env);
  if (!fetched.ok) return {step: "fetch", attempts: fetched.attempts};
  const text = fetched.text;
  const lines = text.split(/\r?\n/);
  const matched = lines.map(s => s.trim()).filter(s => /^\d+\.\d+\.\d+\.\d+$/.test(s));
  let inserted = 0;
  for (const ip of matched) {
    try {
      const r = await env.DB.prepare(
        "INSERT OR IGNORE INTO proxy_ips (ip, status, added_at, updated_at) VALUES (?, 'untested', ?, ?)"
      ).bind(ip, now, now).run();
      if (r.meta.changes) inserted++;
    } catch (e) { return {step: "insert", ip, error: String(e)}; }
  }
  return {textLen: text.length, lines: lines.length, matched: matched.length, inserted, sample: matched.slice(0, 5), first200: text.slice(0, 200)};
}

async function refreshProxyIps(env, ctx) {
  const now = Date.now();
  const meta = await env.DB.prepare(
    "SELECT last_at FROM proxy_refresh_meta WHERE source=?"
  ).bind(PROXY_IP_SOURCE).first();
  if (meta?.last_at && now - meta.last_at < PROXY_REFRESH_MS) return;

  const fetched = await fetchIpdbThroughPool(env);
  if (!fetched.ok) return;   // next cron retries
  const text = fetched.text;

  const ips = text.split(/\r?\n/)
    .map(s => s.trim())
    .filter(s => /^\d+\.\d+\.\d+\.\d+$/.test(s) || /^\[[0-9a-fA-F:]+\]$/.test(s));

  if (ips.length === 0) return;   // don't stamp on empty response

  for (const ip of ips) {
    await env.DB.prepare(
      "INSERT OR IGNORE INTO proxy_ips (ip, status, added_at, updated_at) VALUES (?, 'untested', ?, ?)"
    ).bind(ip, now, now).run();
  }

  // Reset expired cooldowns back to healthy proactively
  await env.DB.prepare(
    "UPDATE proxy_ips SET status='healthy', updated_at=? WHERE status='cooldown' AND cooldown_until < ?"
  ).bind(now, now).run();

  // Reset dead IPs older than 24h back to untested (they may recover)
  await env.DB.prepare(
    "UPDATE proxy_ips SET status='untested', dead_count=0, last_error=NULL, updated_at=? WHERE status='dead' AND updated_at < ?"
  ).bind(now, now - 24 * 3600 * 1000).run();

  // Stamp last_at AFTER successful refresh
  await env.DB.prepare(
    "INSERT INTO proxy_refresh_meta (source, last_at) VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET last_at=excluded.last_at"
  ).bind(PROXY_IP_SOURCE, now).run();
}

// ── Telegram Bot integration ─────────────────────────────
// Reuses the extractor pipeline. Only diff: entry via /tg-webhook,
// and status transitions notify Telegram (edit the "loading" message).

const _tgSeenUpdates = new Map();  // update_id → timestamp; simple in-memory dedup

async function handleTgWebhook(request, env, ctx) {
  const update = await request.json().catch(() => ({}));

  // Dedup by update_id (Telegram may re-send on webhook timeout)
  const uid = update.update_id;
  if (uid != null) {
    if (_tgSeenUpdates.has(uid)) return json({ok: true, dedup: true});
    _tgSeenUpdates.set(uid, Date.now());
    // Cap size
    if (_tgSeenUpdates.size > 200) {
      const cutoff = Date.now() - 300_000;   // 5 min
      for (const [k, v] of _tgSeenUpdates) {
        if (v < cutoff) _tgSeenUpdates.delete(k);
      }
    }
  }

  // Callback query (inline keyboard button press)
  if (update.callback_query) {
    return await handleTgCallback(update.callback_query, env, ctx);
  }

  const msg = update.message || update.edited_message;
  if (!msg?.text || !msg?.chat?.id) return json({ok: true});
  const chatId = msg.chat.id;
  const text = msg.text.trim();

  // Command handling — accept "/cmd", "/cmd@Bot", "/cmd 720p", etc; require no URL in message.
  const cmdMatch = text.match(/^\/(start|help|settings)(?:@\w+)?\b/);
  const isCmd = !!cmdMatch && !extractUrlFromText(text);
  if (isCmd && text.startsWith("/start")) {
    ctx.waitUntil(tgSendMessage(env, chatId,
      "👋 你好!\n\n" +
      "把视频链接发给我 (YouTube / B站 / 抖音 / TikTok / X / 小红书 ...), " +
      "我会解析出直链.\n\n" +
      "常用命令:\n" +
      "  /settings — 设置默认画质\n" +
      "  /help — 帮助\n\n" +
      "支持在链接后加画质标签: URL @1080p"));
    return json({ok: true});
  }
  if (isCmd && text.startsWith("/help")) {
    ctx.waitUntil(tgSendMessage(env, chatId,
      "📖 帮助\n\n" +
      "1. 发送视频链接 (支持 YouTube / B站 / 抖音 / TikTok / X / 小红书 / 快手 / 微博 等)\n" +
      "2. 默认走你设置的画质 (/settings)\n" +
      "3. 想覆盖: 消息里加 @1080p / @720p / @4K / @best / @smallest"));
    return json({ok: true});
  }
  if (isCmd && text.startsWith("/settings")) {
    ctx.waitUntil(sendSettingsMenu(env, chatId, msg.from?.id));
    return json({ok: true});
  }

  const url = extractUrlFromText(text);
  if (!url) {
    ctx.waitUntil(tgSendMessage(env, chatId, "❌ 请发送视频链接, 或 /settings 设置画质, /help 看帮助"));
    return json({ok: true});
  }

  // Quality: inline "@720p" overrides, else user default, else "best"
  // Match only outside the URL to avoid Instagram-style "@username" mis-detection.
  const textOutsideUrl = text.replace(url, " ");
  const qm = textOutsideUrl.match(/@(\d{3,4}p?|best|smallest|4k|2k|fhd|hd|sd|audio_only)\b/i);
  const inline = qm ? qm[1] : null;
  const userDefault = await getUserDefaultQuality(env, chatId, msg.from?.id);
  const quality = inline || userDefault || "best";

  // Send placeholder message to get message_id
  const sent = await tgSendMessage(env, msg.chat.id, "🎬 收到, 正在解析...");
  const messageId = sent?.result?.message_id;
  if (!messageId) return json({ok: true});   // TG API failed, give up

  const jobResponse = await createJob({
    source_url: url,
    quality,
    meta: {
      tg: {
        chat_id: msg.chat.id,
        message_id: messageId,
        user_id: msg.from?.id,
      },
    },
  }, env, ctx);

  // If cache hit, jobResponse is the result inline — notify immediately.
  const body = await jobResponse.json();
  if (body.status === "success" && body.direct_url) {
    ctx.waitUntil(tgEditMessage(env, msg.chat.id, messageId, formatTgResult(body),
      {reply_markup: buildPlayerButtons(body.direct_url, body.filename)}));
    return json({ok: true});
  }
  if (body.error) {
    ctx.waitUntil(tgEditMessage(env, msg.chat.id, messageId, `❌ ${body.error}`));
    return json({ok: true});
  }
  // Fresh job: chain in-request polling for near-realtime progress.
  // Each nudgeJob call runs one step (~1-3s). Cron backs up if we hit wall time.
  if (body.job_id) {
    ctx.waitUntil(chainPollForTg(body.job_id, env, ctx));
  }
  return json({ok: true});
}

// Chain-nudge a fresh job so Telegram sees near-realtime progress updates.
// Poll interval ramps up: 2s → 3s → 5s → 5s ... to feel responsive early,
// then back off to stay under upstream rate limits.
async function chainPollForTg(jobId, env, ctx) {
  const deadline = Date.now() + 26_000;
  // Interval choice: keep ≥3s between vthreads calls to avoid triggering CF 1015.
  // Baseline probe showed 429 kicks in for &lt;1s bursts; 3s+ is stable.
  const intervals = [3000, 4000, 5000];   // then stay at 5s
  let iter = 0;
  let unchangedTicks = 0;
  let lastUpdated = 0;
  while (Date.now() + 10000 < deadline) {
    await sleep(intervals[Math.min(iter, intervals.length - 1)]);
    iter++;
    try {
      const job = await env.DB.prepare(
        "SELECT status, updated_at, owner_expires FROM extractor_jobs WHERE id=?"
      ).bind(jobId).first();
      if (!job) return;
      if (job.status === "success" || job.status === "failed") return;
      if (job.owner_expires && job.owner_expires > Date.now()) continue;
      if (job.updated_at === lastUpdated) {
        if (++unchangedTicks >= 3) return;
      } else {
        unchangedTicks = 0;
        lastUpdated = job.updated_at;
      }
      await nudgeJob(jobId, env, ctx);
    } catch (e) {
      console.warn("chainPollForTg:", e?.message || String(e));
    }
  }
}

// In-memory per-job throttle state. Shared per isolate; loss on cold start is fine.
const _tgEditCache = new Map();   // jobId → {ts, status, progress, message}
const _rateLimitCount = new Map();  // jobId → consecutive 429 count

// Called from nudgeJob at each state transition. If job.meta.tg exists, edit the message.
async function notifyTgIfNeeded(job, env) {
  const meta = safeJson(job.meta, {});
  if (!meta?.tg?.chat_id || !meta?.tg?.message_id) return;
  const {chat_id, message_id} = meta.tg;

  // Rate-limit: skip if same status + same message + progress delta < 5% and < 1500ms.
  // Force edit on terminal status or message text change.
  const prev = _tgEditCache.get(job.id) || {ts: 0, status: null, progress: -100, message: null};
  const now = Date.now();
  const terminal = job.status === "success" || job.status === "failed";
  const p = Math.round(job.ext_progress || 0);
  const msg = job.message || "";
  const messageChanged = msg !== (prev.message || "");
  if (!terminal && !messageChanged) {
    if (prev.status === job.status && Math.abs(p - prev.progress) < 5) return;
    if (now - prev.ts < 1500) return;
  }
  _tgEditCache.set(job.id, {ts: now, status: job.status, progress: p, message: msg});
  // Prune cache if huge (should never happen at self-use scale)
  if (_tgEditCache.size > 500) _tgEditCache.clear();

  let text;
  let opts = {};
  if (job.status === "success") {
    text = formatTgResult({
      title: job.title,
      filename: job.filename,
      direct_url: job.direct_url,
      file_size: job.file_size,
      quality: job.quality_actual,
      platform: job.platform,
      required_headers: safeJson(job.required_headers, {}),
      expires_at: job.expires_at,
    });
    opts.reply_markup = buildPlayerButtons(job.direct_url, job.filename);
  } else if (job.status === "failed") {
    text = `❌ 解析失败\n\n${escapeHtml((job.error || "unknown").slice(0, 300))}`;
  } else if (job.status === "polling") {
    const p = Math.round(job.ext_progress || 0);
    const bar = renderBar(p);
    text = `⏳ 提取中\n\n${bar} ${p}%\n${escapeHtml((job.message || "").slice(0, 100))}`;
  } else {
    text = `⏳ ${escapeHtml(job.status)}...`;
  }
  await tgEditMessage(env, chat_id, message_id, text, opts);
}

// Player list adapted from OpenList/AList VideoBox. Full cross-platform coverage.
// $durl = raw URL; $edurl = URL-encoded; $bdurl = base64 URL; $name = filename.
const PLAYERS = [
  {name: "IINA",         scheme: "iina://weblink?url=$edurl",                                                       platforms: ["MacOS"]},
  {name: "SenPlayer",    scheme: "senplayer://x-callback-url/play?url=$edurl",                                      platforms: ["iOS"]},
  {name: "Infuse",       scheme: "infuse://x-callback-url/play?url=$durl",                                          platforms: ["MacOS","iOS"]},
  {name: "PotPlayer",    scheme: "potplayer://$durl",                                                               platforms: ["Windows"]},
  {name: "VLC",          scheme: "vlc://$durl",                                                                     platforms: ["Windows","MacOS","Linux","Android","iOS"]},
  {name: "Android",      scheme: "intent:$durl#Intent;type=video/*;S.title=$name;end",                              platforms: ["Android"]},
  {name: "nPlayer",      scheme: "nplayer-$durl",                                                                   platforms: ["Android","iOS"]},
  {name: "OmniPlayer",   scheme: "omniplayer://weblink?url=$durl",                                                  platforms: ["MacOS"]},
  {name: "Fig Player",   scheme: "figplayer://weblink?url=$durl",                                                   platforms: ["Windows","MacOS"]},
  {name: "Vivid Player", scheme: "vividplayer://play?src=direct&u=$edurl&title=$name",                              platforms: ["Windows"]},
  {name: "Fileball",     scheme: "filebox://play?url=$durl",                                                        platforms: ["MacOS","iOS"]},
  {name: "MX Player",    scheme: "intent:$durl#Intent;package=com.mxtech.videoplayer.ad;S.title=$name;end",         platforms: ["Android"]},
  {name: "MX Player Pro",scheme: "intent:$durl#Intent;package=com.mxtech.videoplayer.pro;S.title=$name;end",        platforms: ["Android"]},
  {name: "iPlay",        scheme: "iplay://play/any?type=url&url=$bdurl",                                            platforms: ["iOS"]},
  {name: "mpv",          scheme: "mpv://$edurl",                                                                    platforms: ["Windows","MacOS","Linux","Android"]},
];

function b64url(s) {
  // UTF-8 → bytes → btoa → URL-safe. Replaces deprecated unescape().
  const bytes = new TextEncoder().encode(String(s));
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function playerLink(scheme, url, name) {
  // Strip characters that could break out of scheme parsers (Android intent injection,
  // fragment truncation, query-param injection). direct_urls from vthreads have none
  // of these anyway; defensive.
  const safe = String(url).replace(/[#;]/g, "");
  return scheme
    .replace(/\$edurl/g, encodeURIComponent(safe))
    .replace(/\$bdurl/g, b64url(safe))
    .replace(/\$durl/g, safe)
    .replace(/\$name/g, encodeURIComponent(name || "video.mp4"));
}

// Telegram button.url only accepts http/https/tg — we can't put iina:// etc directly.
// Instead each button hits /play?p=<player>&u=<direct_url>&n=<name> which 302s to the scheme.
const PLAY_BASE = "https://extractor.bugcf.ccwu.cc/play";

function buildPlayerButtons(url, filename) {
  if (!url) return undefined;
  const rows = [];
  for (let i = 0; i < PLAYERS.length; i += 2) {
    const row = PLAYERS.slice(i, i + 2).map(p => ({
      text: `▶️ ${p.name}`,
      url: `${PLAY_BASE}?p=${encodeURIComponent(p.name)}&u=${encodeURIComponent(url)}&n=${encodeURIComponent(filename || "video.mp4")}`,
    }));
    rows.push(row);
  }
  return {inline_keyboard: rows};
}

// Only allow /play to redirect to URLs from known upstream domains.
// Prevents extractor.bugcf.ccwu.cc becoming a phishing/malware redirect laundry.
const PLAY_ALLOWED_HOSTS = new Set([
  "vthreads.top",
  "www.vthreads.top",
  "cobalt.tools",
  "api.cobalt.tools",
  // Cobalt has many community-hosted proxies; add as-needed via config
]);

function isAllowedPlayUrl(u) {
  try {
    const parsed = new URL(u);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return false;
    return PLAY_ALLOWED_HOSTS.has(parsed.hostname.toLowerCase());
  } catch { return false; }
}

// GET /play?p=<name>&u=<url>&n=<filename>  → 302 to player scheme
function handlePlayRedirect(url) {
  const p = url.searchParams.get("p");
  const u = url.searchParams.get("u");
  const n = url.searchParams.get("n") || "video.mp4";
  if (!p || !u) return new Response("missing p or u", {status: 400});
  if (!isAllowedPlayUrl(u)) return new Response("url host not allowed", {status: 403});
  const player = PLAYERS.find(x => x.name === p);
  if (!player) return new Response(`unknown player: ${p}`, {status: 404});
  const target = playerLink(player.scheme, u, n);
  return new Response(null, {status: 302, headers: {"Location": target}});
}

function renderBar(pct) {
  const total = 10;
  const filled = Math.max(0, Math.min(total, Math.round(pct / 10)));
  return "█".repeat(filled) + "░".repeat(total - filled);
}

function extractUrlFromText(t) {
  const m = t.match(/https?:\/\/[^\s]+/);
  if (!m) return null;
  let url = m[0];
  // Strip our own quality tag if directly appended (e.g. "url@1080p" no space)
  url = url.replace(/@(\d{3,4}p?|best|smallest|4k|2k|fhd|hd|sd|audio_only)$/i, "");
  // Strip trailing punctuation people commonly put after URLs
  url = url.replace(/[.,;:!?)\]}>」』]+$/, "");
  return url;
}

function escapeHtml(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function humanSize(n) {
  if (!n) return "?";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + " KB";
  return n + " B";
}

function formatTgResult(r) {
  // Bound each field to keep total under Telegram's 4096 byte limit.
  const title = escapeHtml(String(r.title || r.filename || "unknown").slice(0, 200));
  const size = humanSize(r.file_size);
  const q = escapeHtml(String(r.quality || "").slice(0, 40));
  const url = escapeHtml(String(r.direct_url || "").slice(0, 2000));
  const ref = r.required_headers?.Referer;
  const expH = r.expires_at ? Math.max(1, Math.round((r.expires_at - Date.now()) / 3600000)) : 1;
  const refLine = ref ? `\n⚠️ 需带 <code>Referer: ${escapeHtml(String(ref).slice(0, 200))}</code>` : "";
  return `✅ <b>${title}</b>\n\n` +
    `📦 ${size}  ·  🎞️ ${q}\n` +
    `⏰ 有效 ~${expH}h\n\n` +
    `<a href="${url}">🔗 下载直链</a>` + refLine;
}

// ── User preferences ─────────────────────────────────────
// Pref key = (chat_id, user_id). In private chat these are same. In group each
// member has own default. If user_id unknown, fall back to chat_id (backward compat).
async function getUserDefaultQuality(env, chatId, userId) {
  const uid = userId || chatId;
  try {
    // Prefer per-user; fall back to chat-level legacy row.
    let r = await env.DB.prepare(
      "SELECT default_quality FROM user_prefs WHERE chat_id=? AND user_id=?"
    ).bind(chatId, uid).first();
    if (!r) {
      r = await env.DB.prepare(
        "SELECT default_quality FROM user_prefs WHERE chat_id=? AND user_id=? LIMIT 1"
      ).bind(chatId, chatId).first();
    }
    return r?.default_quality || null;
  } catch (_) { return null; }
}
async function setUserDefaultQuality(env, chatId, userId, quality) {
  const uid = userId || chatId;
  // PK is chat_id (legacy); insert-then-update per (chat_id, user_id) requires
  // separate rows. Use INSERT OR REPLACE keyed by chat_id when uid==chat_id (private),
  // else insert new row per user.
  if (uid === chatId) {
    await env.DB.prepare(`
      INSERT INTO user_prefs (chat_id, user_id, default_quality, updated_at) VALUES (?, ?, ?, ?)
      ON CONFLICT(chat_id) DO UPDATE SET default_quality=excluded.default_quality, updated_at=excluded.updated_at, user_id=excluded.user_id
    `).bind(chatId, uid, quality, Date.now()).run();
  } else {
    // Group chat: separate row per user. Delete stale then insert.
    await env.DB.prepare("DELETE FROM user_prefs WHERE chat_id=? AND user_id=? AND chat_id != user_id").bind(chatId, uid).run();
    await env.DB.prepare(
      "INSERT INTO user_prefs (chat_id, user_id, default_quality, updated_at) VALUES (?, ?, ?, ?)"
    ).bind(chatId, uid, quality, Date.now()).run();
  }
}

// ── Settings menu (inline keyboard) ──────────────────────
async function sendSettingsMenu(env, chatId, userId) {
  const current = (await getUserDefaultQuality(env, chatId, userId)) || "best";
  const options = ["best", "2160p", "1440p", "1080p", "720p", "480p", "smallest"];
  const keyboard = options.map(q => ([{
    text: (q === current ? "✅ " : "") + q,
    callback_data: `q:${q}`,
  }]));
  await tgApi(env, "sendMessage", {
    chat_id: chatId,
    text: `⚙️ <b>默认画质设置</b>\n\n当前: <b>${escapeHtml(current)}</b>\n\n选一个:`,
    parse_mode: "HTML",
    reply_markup: {inline_keyboard: keyboard},
  });
}

async function handleTgCallback(cq, env, ctx) {
  const chatId = cq.message?.chat?.id;
  const messageId = cq.message?.message_id;
  const userId = cq.from?.id;
  const data = cq.data || "";
  if (data.startsWith("q:") && chatId && messageId) {
    const q = canonQuality(data.slice(2));
    await setUserDefaultQuality(env, chatId, userId, q);
    ctx.waitUntil(tgApi(env, "answerCallbackQuery", {callback_query_id: cq.id, text: `已设为 ${q}`}));
    ctx.waitUntil(tgApi(env, "editMessageText", {
      chat_id: chatId, message_id: messageId,
      text: `⚙️ <b>默认画质</b>: <b>${escapeHtml(q)}</b>\n\n已保存. 之后发链接会用此画质 (可用 <code>URL @720p</code> 单次覆盖).`,
      parse_mode: "HTML",
    }));
    return json({ok: true});
  }
  ctx.waitUntil(tgApi(env, "answerCallbackQuery", {callback_query_id: cq.id}));
  return json({ok: true});
}

async function tgApi(env, method, body) {
  if (!env.TG_BOT_TOKEN) return null;
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!j.ok) {
      // Log but don't crash
      console.warn("tgApi", method, "→", r.status, j.description);
    }
    return j;
  } catch (e) {
    console.warn("tgApi", method, "err:", e?.message || String(e));
    return null;
  }
}

async function tgSendMessage(env, chat_id, text, opts = {}) {
  return tgApi(env, "sendMessage", {chat_id, text, parse_mode: "HTML", disable_web_page_preview: true, ...opts});
}

async function tgEditMessage(env, chat_id, message_id, text, opts = {}) {
  return tgApi(env, "editMessageText", {chat_id, message_id, text, parse_mode: "HTML", disable_web_page_preview: true, ...opts});
}

// ── scheduled (cron) ─────────────────────────────────────
async function scheduled(env, ctx) {
  const now = Date.now();

  // Refresh proxy IP pool from upstream source (rate-limited to every 30 min).
  ctx.waitUntil(refreshProxyIps(env, ctx));

  const stuck = await env.DB.prepare(`
    SELECT id FROM extractor_jobs
    WHERE status IN ('pending','polling')
      AND (owner_id IS NULL OR owner_expires < ?)
    ORDER BY updated_at ASC
    LIMIT 10
  `).bind(now).all();
  // ctx.waitUntil() returns undefined; wrap actual promises for allSettled to work.
  // Isolate lifetime extended by waitUntil regardless.
  const nudges = (stuck.results || []).map(row => nudgeJob(row.id, env, ctx));
  nudges.forEach(p => ctx.waitUntil(p));
  await Promise.allSettled(nudges);

  // Reclaim archive jobs whose runner crashed / lost lease. Runner heartbeats
  // updated_at every ~2 min; if we see no touch for 20 min the runner is dead
  // → bounce back to pending so the next runner poll picks it up. The 20-min
  // window is generous to avoid double-processing legitimately slow pipelines
  // (a multi-GB YouTube archive can take 15+ min).
  await env.DB.prepare(
    "UPDATE extractor_jobs SET archive_status = 'pending', archive_owner_id = NULL, updated_at = ? " +
    "WHERE archive_status = 'processing' AND updated_at < ?"
  ).bind(now, now - 20 * 60 * 1000).run();

  // GC: soft-delete expired direct_urls (mark status if needed).
  // Hard-delete rows older than 7 days regardless of status.
  await env.DB.prepare(
    "DELETE FROM extractor_jobs WHERE updated_at < ?"
  ).bind(now - 7 * 24 * 3600 * 1000).run();
}
