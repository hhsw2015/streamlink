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

-- (vthreads removed — service paywalled itself in mid-2026.)
