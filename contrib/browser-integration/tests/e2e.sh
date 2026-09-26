#!/usr/bin/env bash
# End-to-end sanity for the streamlink-redirect browser integration.
#
# Runs, in order:
#   1. Python unit tests for the savenow plugin (pytest)
#   2. Native-messaging host protocol round-trip (python)
#   3. Cloud extractor live smoke (python + urllib)
#
# (The old extension canonCloudQuality parity check was dropped: the extension
#  no longer does cloud quality canon - bg.js is ytplay-only now.)
#
# Exit code 0 = everything green. Any red = non-zero.
#
# Uses the project's .venv Python if present, falls back to python3.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"

if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
  PYTEST="$REPO/.venv/bin/pytest"
else
  PY="$(command -v python3)"
  PYTEST="$(command -v pytest || echo)"
fi

echo "==> using PY=$PY"

echo
echo "==> 1/3 pytest tests/plugins/test_savenow.py"
if [[ -n "${PYTEST:-}" ]]; then
  (cd "$REPO" && "$PYTEST" tests/plugins/test_savenow.py -x -q)
else
  echo "  [SKIP] pytest not installed"
fi

echo
echo "==> 2/3 python native_host_protocol.py"
"$PY" "$HERE/native_host_protocol.py"

echo
echo "==> 3/3 python cloud_live.py"
"$PY" "$HERE/cloud_live.py"

echo
echo "==> all checks passed"
