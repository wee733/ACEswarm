#!/usr/bin/env bash
set -eo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
source "${UXRCE_PX4_SETUP:-$HOME/ros2_ws/install/local_setup.bash}"
python_bin="${UXRCE_PYTHON:-python3}"
if [[ -z "${UXRCE_PYTHON:-}" && -x "$HOME/.local/share/uxrce-telemetry-venv/bin/python" ]]; then
  python_bin="$HOME/.local/share/uxrce-telemetry-venv/bin/python"
fi
export UXRCE_EXTRA_HOLD="$root/bin/extra_hold"
export LD_LIBRARY_PATH="$root/lib:${LD_LIBRARY_PATH:-}"
exec "$python_bin" "$root/run.py" "${AIVUDA_APP_CONFIG_PATH:-$root/config/default_config.yaml}"
