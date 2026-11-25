#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$PWD}"
mkdir -p "$out"
package_name="$(python3 - "$root/manifest.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

manifest = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"{manifest['app_id']}-{manifest['version']}.tar.gz")
PY
)"
archive="$out/$package_name"
tar --exclude='__pycache__' -czf "$archive" -C "$root" manifest.yaml start.sh run.py config \
  -C "$root/.." uxrce_telemetry_bridge
echo "$archive"
