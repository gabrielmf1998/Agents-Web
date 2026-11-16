#!/usr/bin/env bash
# Builds every release file, each in the official container image it is meant for (docker or podman):
#   packaging/build-all.sh                          dist/: the .deb, .rpm, pacman package, AppImage, Windows
#                                                   installer, the tarball for install.sh and SHA256SUMS
#   OUT=dist/old VERSION=1.9.9 packaging/build-all.sh   an older build for the update tests
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/${OUT:-dist}"
D="${DOCKER:-$(command -v docker || command -v podman)}"
mkdir -p "$OUT"

run() { # image, targets (the first one names the build tools to install)
    local img="$1" cmd
    shift
    cmd="bash /src/packaging/build.sh deps $1 >/dev/null"
    for t in "$@"; do cmd+=" && bash /src/packaging/build.sh $t"; done
    "$D" run --rm -v "$ROOT:/src:ro" -v "$OUT:/out" -e DIST=/out -e VERSION="${VERSION:-}" "$img" bash -c "$cmd"
}

# the tray first: every Linux file carries this one binary, built on an old glibc so it runs everywhere
run almalinux:8 tray
pids=()
run ubuntu:22.04 appimage tarball deb & pids+=($!)
run almalinux:9 rpm & pids+=($!)
run archlinux:latest pacman & pids+=($!)
run ubuntu:24.04 windows & pids+=($!)
ok=1
for p in "${pids[@]}"; do wait "$p" || ok=0; done
[ "$ok" = 1 ] || { echo "a build failed" >&2; exit 1; }
DIST="$OUT" bash "$ROOT/packaging/build.sh" sums
