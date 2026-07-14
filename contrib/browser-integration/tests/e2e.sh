#!/usr/bin/env bash
# End-to-end sanity for the streamlink-redirect browser integration.
#
# Runs, in order:
#   1. Python unit tests for the vthreads plugin (pytest)
#   2. Extension canonCloudQuality parity vs Python (node)
#   3. Native-messaging host protocol round-trip (python)
#   4. Cloud extractor live smoke (python + urllib)
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
echo "==> 1/4 pytest tests/plugins/test_vthreads*.py"
if [[ -n "${PYTEST:-}" ]]; then
  (cd "$REPO" && "$PYTEST" tests/plugins/test_vthreads.py tests/plugins/test_vthreads_paths.py -x -q)
else
  echo "  [SKIP] pytest not installed"
fi

echo
echo "==> 2/4 node extension_canon.mjs"
if command -v node >/dev/null 2>&1; then
  node "$HERE/extension_canon.mjs"
else
  echo "  [SKIP] node not installed"
fi

echo
echo "==> 3/4 python native_host_protocol.py"
"$PY" "$HERE/native_host_protocol.py"

echo
echo "==> 4/4 python cloud_live.py"
"$PY" "$HERE/cloud_live.py"

echo
echo "==> all checks passed"
