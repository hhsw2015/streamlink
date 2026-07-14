-- Extractor Worker D1 schema
CREATE TABLE IF NOT EXISTS extractor_jobs (
  id                TEXT PRIMARY KEY,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,

  source_url        TEXT NOT NULL,
  quality           TEXT DEFAULT 'best',
  service_hint      TEXT,
  webhook           TEXT,
  webhook_headers   TEXT DEFAULT '{}',   -- JSON, forwarded on webhook POST
  meta              TEXT DEFAULT '{}',    -- JSON, echoed back in webhook

  service           TEXT NOT NULL,

  status            TEXT NOT NULL,        -- pending | polling | success | failed
  ext_task_id       TEXT,
  ext_status        TEXT,
  ext_progress      REAL DEFAULT 0,
  message           TEXT,
  error             TEXT,
  retry_count       INTEGER DEFAULT 0,

  direct_url        TEXT,
  file_size         INTEGER,
  filename          TEXT,
  title             TEXT,
  platform          TEXT,
  format            TEXT,
  quality_actual    TEXT,
  duration_s        INTEGER,
  required_headers  TEXT,
  supports_range    INTEGER,              -- 0/1/NULL
  expires_at        INTEGER,

  owner_id          TEXT,
  owner_expires     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON extractor_jobs(status);
CREATE INDEX IF NOT EXISTS idx_updated ON extractor_jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_expires ON extractor_jobs(expires_at);

CREATE TABLE IF NOT EXISTS upstream_services (
  id                    TEXT PRIMARY KEY,
  base_url              TEXT NOT NULL,
  api_type              TEXT NOT NULL,    -- vthreads | cobalt | metube
  required_headers      TEXT DEFAULT '{}',
  supported_platforms   TEXT DEFAULT '["*"]',
  weight                INTEGER DEFAULT 10,
  enabled               INTEGER DEFAULT 1,
  last_429_at           INTEGER DEFAULT 0,
  consecutive_429       INTEGER DEFAULT 0,
  direct_url_ttl_s      INTEGER DEFAULT 3600,   -- best-guess TTL, override per service
  last_healthy_at       INTEGER DEFAULT 0
);
