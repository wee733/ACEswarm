const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');

const root = path.resolve(__dirname, '..');

test('Android URL normalization matches native PageActivity rules and store origin matching', async () => {
  const { normalizePageUrl, sameOrigin } = await import(pathToFileURL(path.join(root, 'android/web/url.mjs')));

  assert.equal(normalizePageUrl('192.168.1.10:9001'), 'http://192.168.1.10:9001/');
  assert.equal(normalizePageUrl('http://192.168.1.10:9001'), 'http://192.168.1.10:9001/');
  assert.equal(normalizePageUrl('localhost:9001/store'), 'http://localhost:9001/store');
  assert.equal(normalizePageUrl('https://store.example.com:443/apps/demo'), 'https://store.example.com/apps/demo');
  assert.equal(normalizePageUrl('http://[fe80::1]:8080/'), 'http://[fe80::1]:8080/');

  // Reject internal virtual origins, credentials, port 0, underscores, and non-HTTP schemes.
  for (const bad of [
    '',
    '   ',
    'https://workbench.aceswarm.invalid/shell.html',
    'https://apps.aceswarm.invalid/com.example.app',
    'http://user:pass@192.168.1.10/',
    'http://192.168.1.10:0/',
    'http://drone_01:8000/',
    'javascript:alert(1)',
    'blob:https://example.com/uuid',
    'data:text/html,hi',
    'ftp://192.168.1.10/file',
    'file:///etc/passwd',
  ]) {
    assert.equal(normalizePageUrl(bad), '', `expected ${JSON.stringify(bad)} to be rejected`);
  }

  assert.equal(sameOrigin('http://192.168.1.10:9001', 'http://192.168.1.10:9001/'), true);
  assert.equal(sameOrigin('192.168.1.10:9001', 'http://192.168.1.10:9001/store'), true);
  assert.equal(sameOrigin('https://192.168.1.10:9001/', 'http://192.168.1.10:9001/'), false);
  assert.equal(sameOrigin('http://192.168.1.10:9001', 'invalid://host'), false);
});

test('adaptShellCss expands :is() selectors and 8-digit hex colors for Chromium 52', async () => {
  const { adaptShellCss } = await import(pathToFileURL(path.join(root, 'android/web/legacy-css.mjs')));
  const css = fs.readFileSync(path.join(root, 'electron/shell.css'), 'utf8');
  const adapted = adaptShellCss(css);

  assert.equal(adapted.includes(':is('), false);
  assert.equal(/#[a-f\d]{8}\b/i.test(adapted), false);
  assert.match(adapted, /:root\[data-theme="dark"\] \.desktop-bar/);
  assert.match(adapted, /rgba\(36, 61, 56, 0\.200\)/);
});

test('AndroidManifest configures singleTop and configChanges for H16 activities', () => {
  const manifest = fs.readFileSync(path.join(root, 'android/station/AndroidManifest.xml'), 'utf8');
  assert.match(manifest, /android:name="\.WorkbenchActivity"[^>]*android:launchMode="singleTop"/);
  assert.match(manifest, /android:name="\.WorkbenchActivity"[^>]*android:configChanges="[^"]*orientation[^"]*screenSize/);
  assert.match(manifest, /android:name="\.PageActivity"[^>]*android:launchMode="singleTop"/);
  assert.match(manifest, /android:name="\.PageActivity"[^>]*android:configChanges="[^"]*orientation[^"]*screenSize/);
});

test('Android web builder emits Chromium 52 assets when android/web dependencies are installed', () => {
  if (!fs.existsSync(path.join(root, 'android/web/node_modules/esbuild'))) return;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aceswarm-android-web-'));
  try {
    execFileSync(process.execPath, [path.join(root, 'android/web/build.cjs'), dir], { stdio: 'pipe' });
    const html = fs.readFileSync(path.join(dir, 'shell.html'), 'utf8');
    assert.equal(html.includes('dockview-core'), false);
    assert.equal(html.includes('./downloads.js'), false);
    assert.equal(html.includes('./compat.js'), true);
    assert.equal(html.includes('./bridge.js'), true);
    assert.equal(html.includes('./lucide.js'), true);
    assert.equal(html.includes('./h16.css'), true);
    assert.ok(fs.statSync(path.join(dir, 'bridge.js')).size > 0);
    assert.ok(fs.statSync(path.join(dir, 'shell.css')).size > 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
