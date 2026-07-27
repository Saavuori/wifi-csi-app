#!/usr/bin/env bash
# Everything that can be checked without hardware.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-$here/.venv/bin/python}"

echo "== server tests =="
"$python" -m pytest "$here/server/tests" -q

echo
echo "== server lint =="
(cd "$here/server" && "$python" -m ruff check .)

echo
echo "== firmware host tests =="
"$here/firmware/scripts/run_host_tests.sh"

echo
echo "== web typecheck and build =="
(cd "$here/web" && npm run build)

echo
echo "all checks passed"
