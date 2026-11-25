#!/usr/bin/env bash
# Run with the target ROS distribution sourced and its development packages installed.
set -euo pipefail
cd "$(dirname "$0")"
target="${ROS_DISTRO:?source the target ROS setup.bash first}-$(uname -m)"
out="$PWD/backend/native/$target"
build="$(mktemp -d)"
trap 'rm -rf -- "$build"' EXIT
cmake -S native -B "$build" -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE=/usr/bin/python3
cmake --build "$build" --parallel "${RECORDER_BUILD_JOBS:-2}"
mkdir -p "$out"
module=("$build"/_recorder_native*.so)
install -m 755 "${module[0]}" "$out/$(basename "${module[0]}").new"
mv -- "$out/$(basename "${module[0]}").new" "$out/$(basename "${module[0]}")"
