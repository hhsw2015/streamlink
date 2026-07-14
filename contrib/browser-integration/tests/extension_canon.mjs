// Verify the extension's canonCloudQuality matches the Python plugin's mapping,
// which in turn matches worker.js. Run:  node extension_canon.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const bgPath = resolve(here, "..", "chrome-extension", "bg.js");
const src = readFileSync(bgPath, "utf8");

// Extract the canonCloudQuality function body via a robust marker and eval into a Function.
// (No import syntax in bg.js — it's a Chrome service worker script, not a module.)
const marker = "function canonCloudQuality";
const start = src.indexOf(marker);
assert.ok(start >= 0, "bg.js missing canonCloudQuality");
// Grab from marker to the next "\nfunction " (or EOF)
const rest = src.slice(start);
const nextFn = rest.indexOf("\nfunction ", 1);
const body = nextFn >= 0 ? rest.slice(0, nextFn) : rest;
const canon = new Function(body + "\nreturn canonCloudQuality;")();

const cases = [
  ["best", "best"],
  ["worst", "smallest"],
  ["smallest", "smallest"],
  ["audio_only", "audio_only"],
  ["1080p", "1080p"],
  ["1080", "1080p"],
  ["FHD", "1080p"],
  ["1080p Full HD", "1080p"],
  ["4k", "2160p"],
  ["UHD", "2160p"],
  ["2K", "1440p"],
  ["QHD", "1440p"],
  ["HD", "720p"],
  ["SD", "480p"],
  ["720p", "720p"],
  ["", "best"],
  ["garbage", "best"],
];

let failed = 0;
for (const [inp, expected] of cases) {
  const got = canon(inp);
  const ok = got === expected;
  console.log(`  [${ok ? "PASS" : "FAIL"}] canon(${JSON.stringify(inp)}) -> ${JSON.stringify(got)} (want ${JSON.stringify(expected)})`);
  if (!ok) failed++;
}

if (failed) {
  console.error(`${failed} case(s) failed`);
  process.exit(1);
}
console.log("all extension canon tests passed");
