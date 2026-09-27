#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -u) != 0 ]] || { echo 'Run as the dedicated non-root service user, not root.' >&2; exit 1; }
[[ -n ${XDG_RUNTIME_DIR:-} ]] || { echo 'Log in as the service user so XDG_RUNTIME_DIR/user systemd exist.' >&2; exit 1; }
command -v python3 >/dev/null
command -v podman >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
base="$HOME/.xodex"
mkdir -p "$base" "$HOME/.config/systemd/user" "$HOME/.local/bin" "$base/tasks" "$base/state" "$base/run" "$base/secrets"
chmod 700 "$base"
python3 -m venv "$base/venv"
"$base/venv/bin/python" -m pip install "$source_dir"
ln -sfn "$base/venv/bin/xodex" "$HOME/.local/bin/xodex"
[[ -e "$base/config.toml" ]] || install -m 600 "$source_dir/deploy/config.example.toml" "$base/config.toml"
[[ -e "$base/tunnel-client.yaml" ]] || install -m 600 "$source_dir/deploy/tunnel-client.example.yaml" "$base/tunnel-client.yaml"
[[ -e "$base/tunnel.env" ]] || install -m 600 "$source_dir/deploy/tunnel.env.example" "$base/tunnel.env"
install -m 644 "$source_dir"/deploy/systemd/*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
printf '\nInstalled, not started. Next:\n'
printf '1. Edit ~/.xodex/config.toml and provision ~/.xodex/secrets/github-token (0600).\n'
printf '2. Build the worker: ./scripts/build-worker.sh\n'
printf '3. Run: %s/venv/bin/xodex doctor --probe\n' "$base"
printf '4. Install the verified OpenAI tunnel-client release bundle; configure the tunnel ID/key.\n'
printf '5. systemctl --user enable --now xodex-engine xodex-mcp xodex-tunnel\n'
printf 'Enable lingering once as an administrator so these services remain available after logout.\n'
