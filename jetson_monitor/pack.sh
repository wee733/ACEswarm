#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
output="${1:-jetson_monitor-0.1.3.tar.gz}"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$output" \
    manifest.yaml start.sh monitor.py config
printf '%s\n' "$(realpath "$output")"
