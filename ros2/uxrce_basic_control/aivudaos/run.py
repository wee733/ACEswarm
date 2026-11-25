import json
import os
from pathlib import Path
import sys

import yaml


def run(path):
    settings = yaml.safe_load(path.read_text())['uxrce_basic_control']
    os.environ['ROS_DOMAIN_ID'] = str(settings['ros_domain_id'])
    os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
    os.environ.pop('ROS_LOCALHOST_ONLY', None)
    from uxrce_basic_control.node import main

    args = ['--ros-args']
    for name, value in settings.items():
        if name == 'ros_domain_id':
            continue
        if name in ('climb_rate_m_s', 'max_takeoff_height_m', 'xy_speed_m_s', 'max_xy_radius_m', 'min_goto_height_m',
                    'field_east_size_m', 'field_north_size_m'):
            value = float(value)
        args.extend(['-p', f'{name}:={json.dumps(value)}'])
    main(args)


if __name__ == '__main__':
    run(Path(sys.argv[1]))
