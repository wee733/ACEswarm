#!/usr/bin/env bash
# Run after sourcing the native colcon overlay on the target architecture.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$(realpath -m "${1:-$PWD}")"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$out" "$stage/bin" "$stage/lib"
cp -r "$root"/{manifest.yaml,config,start.sh,run.py} "$root/../uxrce_basic_control" "$root/../README.md" "$stage/"
cp "$root/PX4-LICENSE" "$stage/lib/"
if [[ -n "${2:-}" ]]; then
  # Reuse the paired native artifacts from the target's installed package.
  install -m 755 "$2/bin/extra_hold" "$stage/bin/"
  install -m 755 "$2/lib/libpx4_ros2_cpp.so" "$stage/lib/"
else
  install -s "$(ros2 pkg prefix uxrce_basic_control)/lib/uxrce_basic_control/extra_hold" "$stage/bin/"
  install -s "$(ros2 pkg prefix px4_ros2_cpp)/lib/libpx4_ros2_cpp.so" "$stage/lib/"
fi
version="$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["version"])' "$root/manifest.yaml")"
archive="$out/uxrce_basic_control-$version.tar.gz"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --exclude='__pycache__' -czf "$archive" -C "$stage" .
echo "$archive"
