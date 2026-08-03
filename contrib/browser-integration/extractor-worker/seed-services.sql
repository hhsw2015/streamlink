-- Seed default upstream services.
-- savenow.to is the primary upstream. Only supports YouTube reliably;
-- everything else (TikTok/Bilibili/Twitter/Vimeo/...) comes back Failed.
INSERT OR REPLACE INTO upstream_services
  (id, base_url, api_type, required_headers, supported_platforms, weight, enabled, direct_url_ttl_s)
VALUES
  ('savenow', 'https://p.savenow.to', 'savenow',
   '{"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"}',
   '["youtube"]',
   10, 1, 3600);

-- Old vthreads entry (paywalled/dead in mid-2026). Kept disabled so existing
-- deployments don't accidentally route traffic there; remove manually once
-- you're sure no one still points at it.
INSERT OR REPLACE INTO upstream_services
  (id, base_url, api_type, required_headers, supported_platforms, weight, enabled, direct_url_ttl_s)
VALUES
  ('vthreads', 'https://vthreads.top', 'vthreads',
   '{"Referer":"https://vthreads.top/","User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"}',
   '["youtube","bilibili","douyin","tiktok","x","twitter","instagram","facebook","threads","reddit","pinterest","vimeo","snapchat","xiaohongshu","weibo","kuaishou"]',
   0, 0, 3600);
