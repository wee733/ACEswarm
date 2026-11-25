#!/usr/bin/env bash
set -eo pipefail
app_dir="$(cd "$(dirname "$0")" && pwd)"
source "${AIVUDA_APP_HELPERS_ENTRY_PATH:?AivudaOS configuration helper required}"
export PATH="/usr/bin:$PATH"
expand_path() {
    case "$1" in
        '~/'*) printf '%s/%s' "$HOME" "${1:2}" ;;
        /*) printf '%s' "$1" ;;
        *) echo "Expected an absolute or ~/ path: $1" >&2; return 1 ;;
    esac
}
load_setup() {
    local script
    script="$(expand_path "$1")"
    [[ -r "$script" ]] || { echo "Cannot read ROS setup: $script" >&2; return 1; }
    source "$script"
}

distro="$(aivuda_yaml_get ros2_bag_recorder.ros.distro auto)"
base="$(aivuda_yaml_get ros2_bag_recorder.ros.setup_script)"
output="$(aivuda_yaml_get ros2_bag_recorder.recorder.output_dir)"
buffer_mib="$(aivuda_yaml_get ros2_bag_recorder.recorder.write_buffer_mib 64)"
domain="$(aivuda_yaml_get ros2_bag_recorder.ros.ros_domain_id 0)"
sim_time="$(aivuda_yaml_get ros2_bag_recorder.ros.use_sim_time false)"
overlays="$(aivuda_yaml_get ros2_bag_recorder.ros.extra_overlay_setup_scripts '[]')"
if [[ -z "$base" ]]; then
    if [[ "$distro" != auto ]]; then
        base="/opt/ros/$distro/setup.bash"
    elif [[ -n "${ROS_DISTRO:-}" && -r "/opt/ros/$ROS_DISTRO/setup.bash" ]]; then
        base="/opt/ros/$ROS_DISTRO/setup.bash"
    else
        shopt -s nullglob
        candidates=(/opt/ros/*/setup.bash)
        (( ${#candidates[@]} )) || { echo "No ROS 2 setup.bash found under /opt/ros" >&2; exit 1; }
        base="${candidates[-1]}"
    fi
fi
load_setup "$base"
# The helper returns arrays as JSON. Preserve spaces and reject ambiguous line breaks.
overlay_lines="$(/usr/bin/python3 -c '
import json, sys
paths = json.loads(sys.argv[1])
if not isinstance(paths, list) or any(not isinstance(p, str) or not p or any(c in p for c in "\r\n\0") for p in paths):
    sys.exit("Invalid ros.extra_overlay_setup_scripts")
print("\n".join(paths))
' "$overlays")"
while IFS= read -r overlay; do
    [[ -z "$overlay" ]] || load_setup "$overlay"
done <<< "$overlay_lines"
export ROS_DOMAIN_ID="$domain"
args=(--output-dir "$(expand_path "$output")" --buffer-mib "$buffer_mib")
[[ "$sim_time" != true ]] || args+=(--use-sim-time)
exec /usr/bin/python3 -u "$app_dir/backend/server.py" "${args[@]}"
