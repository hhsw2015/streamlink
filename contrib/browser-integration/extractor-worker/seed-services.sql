-- Seed default upstream services.
INSERT OR REPLACE INTO upstream_services
  (id, base_url, api_type, required_headers, supported_platforms, weight, enabled, direct_url_ttl_s)
VALUES
  ('vthreads', 'https://vthreads.top', 'vthreads',
   '{"Referer":"https://vthreads.top/","User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"}',
   '["youtube","bilibili","douyin","tiktok","x","twitter","instagram","facebook","threads","reddit","pinterest","vimeo","snapchat","xiaohongshu","weibo","kuaishou"]',
   10, 1, 3600);
