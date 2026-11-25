"""Render the package's AivudaOS parameters into its static web entry point."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml


def write_atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8', delete=False) as tmp:
        tmp.write(data)
    os.chmod(tmp.name, 0o644)
    os.replace(tmp.name, path)


def render(root, name, config=None):
    default = yaml.safe_load((root / 'config/default_config.yaml').read_text())[name]
    effective = yaml.safe_load(config.read_text()).get(name, {}) if config and config.is_file() else {}
    value = {**default, **effective}
    if name == 'swarm_controlpad':
        payload = {'layout': value['controlpad_default_settings']}
        if payload['layout'] != default['controlpad_default_settings']:
            payload['shippedLayout'] = default['controlpad_default_settings']
    else:
        payload = {'title': value['title'], 'robotLinkTemplate': value['robot_link_template'],
                   'zenoh': {'keyExpr': value['zenoh']['key_expr']}}
        if name == 'comm_status_viewer':
            payload.update(commStatusTimeoutMs=value['comm_status_timeout_ms'], defaultViewMode=value['default_view_mode'])
        else:
            settings = value['card_default_settings']
            templates = {**default['card_default_settings']['templates'], **settings['templates']}
            templates.pop('mocap_uxrce', None)
            selection = dict(settings['selection'])
            if selection.get('manualTemplateKey') == 'mocap_uxrce':
                selection['manualTemplateKey'] = 'uxrce_mocap'
            payload.update(cardTemplateSelection=selection, cardTemplates=templates,
                           heartbeatTimeoutMs=value['heartbeat_timeout_ms'])
    dest = root / 'ui/app-config.js'
    data = 'window.__' + name.upper() + '_CONFIG__ = ' + json.dumps(payload, ensure_ascii=False) + ';\n'
    write_atomic(dest, data)
    if name in ('swarm_dashboard', 'swarm_controlpad'):
        # Installed WebViews can still cache the unversioned config from older packages.
        revision = hashlib.sha256(data.encode()).hexdigest()[:16]
        index = root / 'ui/index.html'
        html, count = re.subn(r'src="app-config\.js(?:\?v=[0-9a-f]+)?"',
                              f'src="app-config.js?v={revision}"', index.read_text())
        if count != 1:
            raise ValueError('Missing or duplicate application configuration script')
        write_atomic(index, html)


if __name__ == '__main__':
    render(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]) if len(sys.argv) > 3 else None)
