// Build the pinned Store SPA for WebView 52. Its router and same-origin API
// paths stay identical to the desktop implementation.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const ui = path.resolve(here, '../../aivudaAppStore/aivudaappstore/resources/ui');
const target = path.resolve(process.argv[2], 'store');
const upstream = createRequire(path.join(ui, 'package.json'));
const { build } = await import(pathToFileURL(path.join(ui, 'node_modules/vite/dist/node/index.js')));
const entry = path.join(ui, 'src/main.js');
await build({
  root: ui,
  configFile: path.join(ui, 'vite.config.js'),
  base: '/_aceswarm_store/',
  define: { 'process.env.NODE_ENV': '"production"' },
  build: {
    lib: { entry, name: 'AivudaAppStore', formats: ['iife'], fileName: () => 'workbench.js' },
    rollupOptions: { output: { inlineDynamicImports: true, assetFileNames: 'workbench[extname]' } },
    outDir: target, emptyOutDir: true, target: 'chrome52', cssTarget: 'chrome52',
  },
});

// Load polyfills before any Vite helpers capture ES2017 built-ins. In
// Chromium 52 Object.getOwnPropertyDescriptors is undefined until core-js runs.
const polyfills = await upstream('esbuild').build({
  entryPoints: [createRequire(import.meta.url).resolve('core-js/stable')],
  bundle: true, format: 'iife', target: 'chrome52', minify: true, write: false,
});
const scriptPath = path.join(target, 'workbench.js');
const application = await fs.readFile(scriptPath, 'utf8');
await fs.writeFile(scriptPath, polyfills.outputFiles[0].text + '\n;' + application);

const stylePath = path.join(target, 'workbench.css');
const postcss = upstream('postcss');
const styles = postcss.parse(await fs.readFile(stylePath, 'utf8'));
// Pre-layer WebViews drop the entire contents of @layer.
styles.walkAtRules('layer', layer => {
  if (layer.nodes) layer.replaceWith(...layer.nodes);
  else layer.remove();
});
const { transform } = upstream('lightningcss');
const compiled = transform({ filename: 'workbench.css', code: Buffer.from(styles.toString()),
  targets: { chrome: 52 << 16 }, minify: true }).code.toString();
const css = postcss.parse(compiled);
css.walkDecls(/^(margin|padding)-(inline|block)$/, declaration => {
  const [property, axis] = declaration.prop.split('-');
  const [first, second] = postcss.list.space(declaration.value);
  const sides = axis === 'inline' ? ['left', 'right'] : ['top', 'bottom'];
  declaration.cloneBefore({ prop: property + '-' + sides[0], value: first });
  declaration.cloneBefore({ prop: property + '-' + sides[1], value: second || first });
});
css.walkDecls('gap', declaration => {
  declaration.cloneBefore({ prop: '--ace-h16-gap' });
});
css.walkRules(rule => {
  const dark = ':where(.dark,.dark *)';
  if (rule.selector.includes(dark)) {
    rule.selectors = rule.selectors.map(selector => selector.includes(dark)
      ? '.dark ' + selector.replace(dark, '') : selector);
  }
});
await fs.writeFile(stylePath, css.toString() + '\n' + await fs.readFile(path.join(here, 'h16.css'), 'utf8'));

const html = (await fs.readFile(path.join(ui, 'index.html'), 'utf8'))
  .replace(/<link\b[^>]*fonts\.(?:googleapis|gstatic)[^>]*>/gs, '')
  .replace('</head>', '<link rel="stylesheet" href="/_aceswarm_store/workbench.css"></head>')
  .replace('<script type="module" src="/src/main.js"></script>', '<script src="/_aceswarm_store/workbench.js"></script>');
await fs.writeFile(path.join(target, 'index.html'), html);
