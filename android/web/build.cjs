const fs = require('node:fs');
const path = require('node:path');
const esbuild = require('esbuild');
const { pathToFileURL } = require('node:url');

function replaceRequired(source, pattern, replacement, label) {
  const next = source.replace(pattern, replacement);
  if (next === source) throw new Error(`Expected ${label} in electron/shell.html`);
  return next;
}

async function main() {
  if (!process.argv[2]) throw new Error('Usage: node android/web/build.cjs ASSETS_DIRECTORY');
  const target = path.resolve(process.argv[2]);
  const source = path.resolve(__dirname, '../../electron');
  const { adaptShellCss } = await import(pathToFileURL(path.join(__dirname, 'legacy-css.mjs')));
  fs.mkdirSync(path.join(target, 'assets'), { recursive: true });
  let html = fs.readFileSync(path.join(source, 'shell.html'), 'utf8');
  // The Android host owns guest windows and Downloads. All other original
  // script tags retain their source order, including global renderer bindings.
  html = replaceRequired(html, /<script src="[^"]*dockview-core\.min\.js"><\/script>/, '', 'dockview-core script tag');
  html = replaceRequired(html, '<script src="./downloads.js"></script>', '', 'downloads.js script tag');
  html = replaceRequired(
    html,
    '<script src="../node_modules/lucide/dist/umd/lucide.min.js"></script>',
    '<script src="./compat.js"></script>\n  <script src="./bridge.js"></script>\n  <script src="./lucide.js"></script>',
    'lucide script tag',
  );
  html = replaceRequired(html, '</head>', '<link rel="stylesheet" href="./h16.css" /></head>', '</head> tag');
  fs.writeFileSync(path.join(target, 'shell.html'), html);
  fs.writeFileSync(path.join(target, 'shell.css'), adaptShellCss(fs.readFileSync(path.join(source, 'shell.css'), 'utf8')));
  fs.copyFileSync(path.join(__dirname, 'h16.css'), path.join(target, 'h16.css'));
  fs.copyFileSync(path.join(source, 'assets/aivuda_icon.png'), path.join(target, 'assets/aivuda_icon.png'));
  for (const [, name] of html.matchAll(/<script src="\.\/([\w-]+)\.js"><\/script>/g)) {
    let file = path.join(source, name + '.js');
    if (name === 'bridge' || name === 'compat') file = path.join(__dirname, name + '.js');
    if (name === 'lucide') file = path.join(__dirname, 'node_modules/lucide/dist/umd/lucide.min.js');
    await esbuild.build({
      entryPoints: [file], outfile: path.join(target, name + '.js'),
      bundle: name === 'bridge' || name === 'compat', target: 'chrome52', legalComments: 'eof',
      // Keep global script bindings shared across the upstream renderer files.
      minifyWhitespace: true,
    });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
