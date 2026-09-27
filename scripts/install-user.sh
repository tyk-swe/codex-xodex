#!/usr/bin/env bash
set -euo pipefail
umask 077
install_only=false
case "${1:-}" in
  --install-only) install_only=true; shift ;;
  --help|-h) printf 'Usage: %s [--install-only]\n' "$0"; exit 0 ;;
esac
[[ $# == 0 ]] || { echo 'Usage: install-user.sh [--install-only]' >&2; exit 2; }
if [[ $install_only == false && ( ! -t 0 || ! -t 1 ) ]]; then
  echo 'Guided setup requires an interactive terminal. Use --install-only to bootstrap without prompts.' >&2
  exit 1
fi
[[ $(id -u) != 0 ]] || { echo 'Run as the dedicated non-root service user, not root.' >&2; exit 1; }
command -v python3 >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
base="$HOME/.xodex"
launcher="$HOME/.local/bin/xodex"
if [[ -e $launcher || -L $launcher ]]; then
  [[ -L $launcher && $(readlink -- "$launcher") == "$base/venv/bin/xodex" ]] || {
    printf 'Existing launcher conflicts; review it and move it aside manually: %s\n' "$launcher" >&2; exit 1;
  }
fi
[[ ! -L $base && ! -L $base/venv ]] || { echo 'Installation directories must not be symlinks.' >&2; exit 1; }
mkdir -p "$base" "$HOME/.local/bin"
python3 -m venv "$base/venv"
"$base/venv/bin/python" -m pip install "$source_dir"
[[ -L $launcher ]] || ln -s "$base/venv/bin/xodex" "$launcher"
if [[ $install_only == true ]]; then
  printf '\nInstalled. Run %s setup in an interactive terminal.\n' "$launcher"
else
  exec "$launcher" setup
fi
