# Extractor Worker (snapshot)

Cloudflare Worker that fronts vthreads.top with a shared cache and rotating
Cloudflare edge IPs. Used as the **cloud path** by both the browser extension
and the vthreads Streamlink plugin.

Live endpoint: `https://extractor.bugcf.ccwu.cc`
API spec: `docs/extractor.md` in the source repo

## Not the source of truth

This directory is a **snapshot** copied from the primary repo at:

```
https://github.com/hhsw2015/s3-balance  →  extractor-worker/
```

Original commit at time of copy: `f746b7220e77866f1d075620f38dca2bc8503e28`

Bugfixes and new features happen there. This copy is here so anyone
cloning this Streamlink fork can:
- read the worker source alongside the plugin/extension that calls it
- redeploy their own instance without needing the other repo

To sync, `rsync` from the source repo and update this README's commit hash.

## Deploy your own

```bash
cd contrib/browser-integration/extractor-worker
cp wrangler.toml.example wrangler.toml           # then edit the D1 id
npx wrangler d1 create extractor                 # copy the printed id → wrangler.toml
npx wrangler d1 execute extractor --file=schema.sql
npx wrangler d1 execute extractor --file=seed-services.sql
npx wrangler secret put AUTH_TOKEN               # picks the shared X-Auth token
npx wrangler deploy
```

Then in `chrome-extension/bg.js` and `src/streamlink/plugins/vthreads.py`
replace `CLOUD_BASE` / `CLOUD_TOKEN` with your endpoint + token.

## What's here

```
extractor-worker/
├── README.md              # this file
├── wrangler.toml.example  # cloudflare wrangler config (secrets/id redacted)
├── schema.sql             # D1 schema (extractor_jobs + upstream_services)
├── seed-services.sql      # initial upstream list (vthreads)
└── src/worker.js          # request handler + job state machine
```
