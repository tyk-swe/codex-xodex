#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) != 0 ]] || { echo 'Build in the same rootless Podman account that runs Xodex.' >&2; exit 1; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=$(python3 -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["__version__"])' "$root/src/xodex/__init__.py")
image="localhost/chatgpt-xodex-worker:$version"
args=()
[[ -z ${BASE_IMAGE:-} ]] || args+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
[[ -z ${RUST_TOOLCHAIN:-} ]] || args+=(--build-arg "RUST_TOOLCHAIN=$RUST_TOOLCHAIN")
podman build "${args[@]}" -t "$image" -f "$root/worker/Containerfile" "$root/worker"
podman image inspect --format '{{.Id}}' "$image"
