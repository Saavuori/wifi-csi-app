#!/usr/bin/env bash
# Everything that can be checked without hardware.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-$here/.venv/bin/python}"

echo "== server tests =="
"$python" -m pytest "$here/server/tests" -q

echo
echo
echo "== pi node tests =="
# Needs the server package installed: these parse the node's own datagrams with the server's
# protocol module, which is what keeps the two copies of the wire header in step.
"$python" -m pytest "$here/pi/tests" -q

echo
echo "== server lint =="
(cd "$here/server" && "$python" -m ruff check .)

echo
echo "== pi node lint =="
"$python" -m ruff check --config "$here/server/pyproject.toml" "$here/pi"

echo
echo "== firmware host tests =="
"$here/firmware/scripts/run_host_tests.sh"

echo
echo "== web typecheck and build =="
(cd "$here/web" && npm run build)

echo
echo "all checks passed"
