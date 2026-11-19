// shell.css uses :is() only for groups of simple alternatives. Expand those
// selectors so upstream theme changes also reach the Chromium 52 WebView.
export function adaptShellCss(source) {
  return source.replace(/^([^{}\n]*)\s+:is\(([^(){}]+)\)(?=\s*\{)/gm,
    (_, prefix, alternatives) => alternatives.split(',').map(item => prefix + ' ' + item.trim()).join(', '))
    .replace(/#([a-f\d]{8})\b/gi, (_, hex) => {
      const [r, g, b, alpha] = hex.match(/../g).map(pair => parseInt(pair, 16));
      return `rgba(${r}, ${g}, ${b}, ${(alpha / 255).toFixed(3)})`;
    });
}
