#!/usr/bin/env bash
set -e -o pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ros_setup="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
overlay="${HOME}/ros2_ws/install/local_setup.bash"
sdk_python="${HOME}/.local/share/uxrce-telemetry-venv/bin/python"
for path in "$ros_setup" "$overlay" "$sdk_python"; do
  if [[ ! -r "$path" ]]; then echo "Missing telemetry runtime dependency: $path" >&2; exit 1; fi
done
source "$ros_setup"
source "$overlay"
exec "$sdk_python" "$root/run.py" "${AIVUDA_APP_CONFIG_PATH:-$root/config/default_config.yaml}"
