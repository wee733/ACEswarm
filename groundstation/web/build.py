"""Build the web apps on top of the shipped, hash-pinned Panel Hub compatible UIs."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile

import yaml

BASE = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PACKAGES = BASE / 'resources/seed-apps/packages'
OUTPUT = BASE / 'resources/experiment-apps'
CONFIGS = Path.home() / 'Documents/ACEswarm_templates/dashboard'
INPUTS = {
    'swarm_dashboard': ('app_swarm_dashboard_2026042621_5e65-0.2.3', 'db10324a18301c3a38ec153a72d654aff6ebc8484d49ca0aa13cb32121dec29e'),
    'comm_status_viewer': ('app_comm_status_viewer_2026042816_bc6b-0.2.4', '4d9ae4a419d4c959daf8c9fe3b76564630ad34eda3e9441c74adc9f4a2f1ad0e'),
    'swarm_controlpad': ('app_swarm_controlpad_2026043022_d04b-0.2.3', '088db206ee99f629cf747d33b7269eeed5deac6f1c5b1ed30fbdedc92016cae4'),
}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('Unexpected upstream UI at: ' + old)
    return source.replace(old, new)


def adapt_bundle(root, name):
    path, = (root / 'ui/assets').glob('main.*.js')
    script = path.read_text()
    transport = '(' + (HERE / 'gateway-session.js').read_text().strip() + ')'
    stream_path = '/zenoh/api/zenoh/stream'
    script = replace_once(script, '/zenoh_remote_api/ws', stream_path)
    if name == 'swarm_dashboard':
        migrate = (HERE / 'migrate-battery-freshness.js').read_text().strip()
        script = replace_once(script, 'if(t)return C(JSON.parse(t))',
                              'const settings=(' + migrate + ')(t?JSON.parse(t):null,window.localStorage,e);'
                              'if(settings)return C(settings)')
        script = replace_once(script, 'a.Nn.open(new a.TS(t.zenoh.websocketLocator))',
                              transport + '(t.zenoh.keyExpr + ",*/telemetry/**")')
        # Keep other saved templates when retiring the experimental mocap type.
        script = replace_once(script, 'g=(0,r.aE)(r.oe)??h;return{',
                              'g=(0,r.aE)(r.oe)??h;'
                              'const templates={...h.templates,...g.templates};'
                              'delete templates.mocap_uxrce;'
                              'if(g.selection.manualTemplateKey==="mocap_uxrce") '
                              'g.selection.manualTemplateKey="uxrce_mocap";return{')
        script = replace_once(script, 'cardTemplates:g.templates', 'cardTemplates:templates')
        script = replace_once(script, 't.visibleRobotTypes.filter(e=>"string"==typeof e)',
                              't.visibleRobotTypes.filter(e=>"string"==typeof e)'
                              '.map(type=>type==="mocap_uxrce"?"uxrce_mocap":type)')
        # A named partial library adds templates to the editor's current draft.
        script = replace_once(script, '("templates"in r||"default"in r)?(0,b.pz)(r)',
                              '("templates"in r||"default"in r)?(0,b.pz)('
                              'r.templates&&!("default"in r.templates)&&!("selection"in r)'
                              '?{templates:{...Q.templates,...r.templates},selection:Q.selection}:r)')
        # A missing or invalid battery sample has no percentage.
        script = replace_once(script, 'children:Ht(g)', 'children:/[0-9]/.test(e.valueText)?Ht(g)+"%":"—"')
        # Timeout uses elapsed time on this station even if the flight computer clock differs.
        script = replace_once(script, '"number"==typeof r.timestamp_unix_ms&&Number.isFinite(r.timestamp_unix_ms)?r.timestamp_unix_ms:Date.now()', 'Date.now()')
    elif name == 'comm_status_viewer':
        script = replace_once(script, 'a.Nn.open(new a.TS(e.zenoh.websocketLocator))', transport + '(e.zenoh.keyExpr)')
    else:
        script = replace_once(script, 'const t=new i.TS(e.zenoh.websocketLocator||"ws://localhost:10000");', '')
        script = replace_once(script, 'const a=await i.Nn.open(t);if(!r)return void await a.close();s.current=a,',
                              'const session=await ' + transport + '("*/heartbeat",()=>{if(r){n("error");l("Zenoh 事件流已断开");t=window.setTimeout(a,1000)}});'
                              'if(!r)return void await session.close();s.current=session,')
        script = replace_once(script, 'if(t instanceof c.vO)', 'if(t.gatewayError || t instanceof c.vO)')
        migration = (HERE / 'controlpad-layout.js').read_text().strip()
        script = replace_once(script, 'a=v(e?.layout);return{',
                              'upgrade=(' + migration + '),a=upgrade(v(e?.layout),v(e?.shippedLayout||e?.layout));return{')
        script = replace_once(script, 'defaultLayout:a,layout:r||a', 'defaultLayout:a,layout:upgrade(r,a)')
        # Include the actual PX4 reply (including a rejection) in the visible notification.
        for summary in ['`${l} error repl${l>1?"ies":"y"} received.`',
                        '`${a} success repl${a>1?"ies":"y"}, ${l} error repl${l>1?"ies":"y"}.`',
                        '`${a} repl${a>1?"ies":"y"} received.`']:
            script = replace_once(script, 'description:' + summary, 'description:i.trim()')
        script = replace_once(script, 'N[e.zenohOptions.priority]', 'e.zenohOptions.priority.toLowerCase()')
        script = replace_once(script, 'z[e.zenohOptions.consolidation]', 'e.zenohOptions.consolidation.toLowerCase()')
        script = script.replace('PUT ${n} success', 'PUT ${n} 已发送，未确认执行')
        script = script.replace('GET ${n} success', 'GET ${n} 收到节点回复')
        script = script.replace('No reply was received. This usually means no matching zenoh key/queryable responded before timeout.',
                                '机载控制服务未回执，无法确认执行。')
    # The display locator, unlike the SDK config in the vendor bundle, is now an HTTP path.
    script, count = re.subn(r'websocketLocator:n\?\.trim\(\)\?\w\(n\):new URL\([^;]{1,20}?\)\.toString\(\)',
                            'websocketLocator:"' + stream_path + '"', script)
    if count != 1:
        raise ValueError('Missing configuration locator in ' + name)
    script = re.sub(r'\n?//# sourceMappingURL=main\.[^\n]+', '', script)
    # A new asset name invalidates browser and Panel Hub caches after upgrading.
    new_path = path.with_name('main.' + hashlib.sha256(script.encode()).hexdigest()[:12] + '.js')
    new_path.write_text(script)
    index = root / 'ui/index.html'
    html = replace_once(index.read_text(), path.name, new_path.name)
    if name in ('swarm_dashboard', 'swarm_controlpad'):
        # qiankun executes entry scripts inside the same sandbox as the UI.
        html = replace_once(html, '<script defer', '<script src="app-config.js"></script><script defer')
    index.write_text(html)
    path.unlink()
    for mapping in path.parent.glob('*.js.map'):
        mapping.unlink()
    subprocess.run(['node', '--check', str(new_path)], check=True)


def dump(path, value):
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))


def build(root, name):
    filename, digest = INPUTS[name]
    version = {'swarm_dashboard': '0.3.5', 'swarm_controlpad': '0.3.2'}.get(name, '0.3.0')
    archive = PACKAGES / (filename + '.tar.gz')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError('Upstream archive checksum changed: ' + str(archive))
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter='data')
    manifest = root / 'manifest.yaml'
    meta = yaml.safe_load(manifest.read_text())
    meta['version'] = version
    if meta.pop('caddyfile_config_path', None):
        (root / 'config/jetson-heartbeat.caddy').unlink(missing_ok=True)
    dump(manifest, meta)
    default_path = root / 'config/default_config.yaml'
    defaults = yaml.safe_load(default_path.read_text())
    defaults.pop('zenoh', None)
    defaults[name].get('zenoh', {}).pop('websocket_locator', None)
    if name == 'swarm_dashboard':
        defaults[name]['card_default_settings'] = json.loads((CONFIGS / 'swarm-dashboard-card-settings.json').read_text())
    elif name == 'swarm_controlpad':
        defaults[name].pop('zenoh', None)
        defaults[name]['controlpad_default_settings'] = json.loads((CONFIGS / 'swarm-controlpad-uxrce.json').read_text())
    dump(default_path, defaults)
    schema_path = root / 'config/config_schema.yaml'
    schema = yaml.safe_load(schema_path.read_text())
    # AivudaOS drops retired parameters only for objects with a closed property list.
    del schema['properties']['zenoh']
    schema['required'] = [name]
    own = schema['properties'][name]
    if name == 'swarm_controlpad':
        del own['properties']['zenoh']
        own['required'] = [key for key in own.get('required', []) if key != 'zenoh']
    else:
        settings = own['properties']['zenoh']
        settings['properties'].pop('websocket_locator', None)
        settings['additionalProperties'] = False
        settings['required'] = ['key_expr']
    dump(schema_path, schema)
    shutil.copy2(HERE / 'render-config.py', root / 'scripts/render-config.py')
    start = root / 'start.sh'
    tail = start.read_text().split('# Caddy serves assets', 1)[1]
    start.write_text('#!/usr/bin/env bash\nset -euo pipefail\nSCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
                     + f'python3 "$SCRIPT_DIR/scripts/render-config.py" "$SCRIPT_DIR" {name} "${{AIVUDA_APP_CONFIG_PATH:-}}"\n'
                     + '# Caddy serves assets' + tail)
    adapt_bundle(root, name)
    subprocess.run(['python3', str(root / 'scripts/render-config.py'), str(root), name], check=True)
    pack = root / 'pack.sh'
    pack.write_text('#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")"\n'
                    'out="$(realpath -m "${1:-artifacts}")"\nmkdir -p "$out"\n'
                    + f'tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$out/{meta["app_id"]}-{version}.tar.gz"'
                    + ' README.md manifest.yaml pack.sh start.sh assets config scripts ui\n')
    (root / 'README.md').write_text(f'# {meta["name"]} {version}\n\n'
        '订阅通过同源 Zenoh Bridge 2.3.0 网关的 /zenoh/api/zenoh/stream。'
        '独立页面与 Panel Hub 子应用共用相同的应用入口。'
        '配置、卡片与用户布局可在原设置编辑器中管理。\n')
    subprocess.run(['bash', str(pack), str(OUTPUT)], check=True)
    return OUTPUT / (meta['app_id'] + '-' + version + '.tar.gz')


if __name__ == '__main__':
    for key in INPUTS:
        with tempfile.TemporaryDirectory() as tmp:
            print(build(Path(tmp), key))
