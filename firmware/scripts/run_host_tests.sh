#!/usr/bin/env bash
# Compile and run the parts of the firmware that do not need ESP-IDF: the SPSC ring and the
# wire header layout. These are the two places where a mistake is invisible on the device —
# a torn frame reads as noise, a padding byte reads as packet loss.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

cc -O2 -Wall -Wextra -Werror -pthread -I"$here/../main" \
   -o "$out/test_ring" "$here/test_ring.c" "$here/../main/csi_ring.c"
"$out/test_ring"
