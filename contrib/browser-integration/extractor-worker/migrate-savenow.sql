-- Migration: add savenow_keys table + swap the primary upstream service.
-- Idempotent: safe to run against an existing D1 that already has schema.sql applied.

CREATE TABLE IF NOT EXISTS savenow_keys (
  api_key         TEXT PRIMARY KEY,
  email           TEXT,
  password        TEXT,
  balance_micro   INTEGER DEFAULT 0,
  retired         INTEGER DEFAULT 0,
  created_at      INTEGER,
  last_used_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_savenow_keys_active
  ON savenow_keys(retired, balance_micro);

-- Disable vthreads (dead / paywalled). Insert-or-replace ensures the row
-- exists but shows up as enabled=0 weight=0 in service lists.
INSERT OR REPLACE INTO upstream_services
  (id, base_url, api_type, required_headers, supported_platforms, weight, enabled, direct_url_ttl_s)
VALUES
  ('vthreads', 'https://vthreads.top', 'vthreads',
   '{"Referer":"https://vthreads.top/","User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"}',
   '["youtube","bilibili","douyin","tiktok","x","twitter","instagram","facebook","threads","reddit","pinterest","vimeo","snapchat","xiaohongshu","weibo","kuaishou"]',
   0, 0, 3600);

-- Add savenow as the primary upstream. YouTube-only per empirical tests.
INSERT OR REPLACE INTO upstream_services
  (id, base_url, api_type, required_headers, supported_platforms, weight, enabled, direct_url_ttl_s)
VALUES
  ('savenow', 'https://p.savenow.to', 'savenow',
   '{"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"}',
   '["youtube"]',
   10, 1, 3600);
