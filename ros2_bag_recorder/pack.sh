#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
archive="${1:-ros2_bag_recorder-0.2.2.tar.gz}"
staging="$(mktemp -d)"
trap 'rm -rf -- "$staging"' EXIT
mkdir -p "$staging/backend"
cp -r manifest.yaml start.sh pack.sh build_native.sh native assets config ui "$staging/"
cp backend/*.py "$staging/backend/"
for target in jazzy-aarch64 lyrical-x86_64; do
    modules=(backend/native/"$target"/_recorder_native*.so)
    [[ -f "${modules[0]}" ]] || { echo "Missing native build for $target" >&2; exit 1; }
    mkdir -p "$staging/backend/native/$target"
    cp "${modules[@]}" "$staging/backend/native/$target/"
done
tar -czf "$archive" -C "$staging" .
printf '%s\n' "$(realpath "$archive")"
