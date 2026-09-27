#!/usr/bin/env bash
set -euo pipefail
# Compatibility entry point; implementation and build context ship in the wheel.
xodex_bin=${XODEX_BIN:-$(command -v xodex || printf '%s' "$HOME/.local/bin/xodex")}
exec "$xodex_bin" build-worker "$@"
