import json
import os
from pathlib import Path
import sys

import yaml


def run(path):
    settings = yaml.safe_load(path.read_text(encoding='utf-8'))['uxrce_telemetry']
    os.environ['ROS_DOMAIN_ID'] = str(settings['ros_domain_id'])
    os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
    os.environ.pop('ROS_LOCALHOST_ONLY', None)
    from uxrce_telemetry_bridge.node import main
    args = ['--ros-args']
    for name, value in settings.items():
        if name == 'stale_timeout_sec':
            for stream, seconds in value.items():
                args.extend(['-p', f'stale_timeout_sec.{stream}:={float(seconds)}'])
        elif name != 'ros_domain_id':
            if name.endswith('_hz') or name == 'state_timeout_s':
                value = float(value)
            args.extend(['-p', name + ':=' + json.dumps(value)])
    main(args)


if __name__ == '__main__':
    run(Path(sys.argv[1]))
