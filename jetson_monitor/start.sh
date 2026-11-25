#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 - "$root" "${AIVUDA_APP_CONFIG_PATH:-$root/config/default_config.yaml}" <<'PY'
import os
import shutil
import sys
from pathlib import Path
import yaml

root, config = map(Path, sys.argv[1:])
settings = yaml.safe_load(config.read_text())['jetson_monitor']
# JetPack may install jtop in its own virtual environment without PyYAML.
python = os.environ.get('JTOP_PYTHON')
if not python:
    executable = shutil.which('jtop')
    if executable:
        with open(executable, 'rb') as script:
            shebang = script.readline().decode(errors='ignore').strip()
        candidate = shebang[2:] if shebang.startswith('#!') else ''
        if candidate.startswith('/') and os.access(candidate, os.X_OK) and 'python' in Path(candidate).name:
            python = candidate
if not python:
    known = '/opt/jetson-stats/bin/python'
    python = known if os.access(known, os.X_OK) else sys.executable
os.execv(python, [python, str(root / 'monitor.py'), '--agent-id', settings['agent_id'],
                  '--interval', str(settings['interval_s']),
                  '--clocks-mode', settings.get('clocks_mode', 'keep'),
                  '--fan-profile', settings.get('fan_profile', 'keep'),
                  '--fan-speed-pct', str(settings.get('fan_speed_pct', 100))])
PY
