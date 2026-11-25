// Add Jetson metrics to one exported Swarm Dashboard card, retaining its layout.
// node patch-dashboard.cjs settings.json [robot_type] > updated-settings.json
function addJetsonMetrics(settings, robotType = 'uxrce_mocap') {
  const card = Array.isArray(settings.widgets) ? settings : (settings.templates || settings)[robotType];
  if (!card || !Array.isArray(card.sources) || !Array.isArray(card.widgets)) {
    throw new Error('Missing Dashboard card template: ' + robotType);
  }

  const sourceId = 'source_jetson';
  const key = '*/telemetry/jetson';
  const source = card.sources.find(item => item.id === sourceId);
  if (source && source.keyExprTemplate !== key) {
    throw new Error('The card already uses ' + sourceId + ' for another topic');
  }
  if (!source) {
    card.sources.push({ id: sourceId, keyExprTemplate: key, payloadFormat: 'json', freshnessMs: 5000 });
  }

  const format = (field, digits) =>
    '{{default(fixed(default(source_jetson.data.' + field + ', "—"), ' + digits + '), "—")}}';
  const metrics = [
    ['status', 'Jetson', '{{if(eq(source_jetson.data.status, "ok"), "在线", if(eq(source_jetson.data.status, "unavailable"), "采集异常", "无新数据"))}}'],
    ['cpu', 'CPU (%)', format('cpu_pct', 0)],
    ['gpu', 'GPU (%)', format('gpu_pct', 0)],
    ['ram', 'RAM (%)', format('ram_pct', 0)],
    ['temp', '芯片 (°C)', format('temp_soc_c', 1)],
    ['power', '功耗 (W)', format('power_w', 1)],
  ];
  const missing = metrics.filter(([id]) => !card.widgets.some(item => item.id === 'jetson_' + id));
  const width = card.gridCols || 24;
  const columns = Math.min(3, width);
  const height = Math.max(1, Math.ceil(20 / (card.gridRowHeight || 2)));
  const bottom = card.widgets.reduce((end, widget) => Math.max(end, widget.layout.y + widget.layout.h), 0);
  for (const [index, [id, label, template]] of missing.entries()) {
    const column = index % columns;
    const x = Math.floor(column * width / columns);
    const w = Math.floor((column + 1) * width / columns) - x;
    const y = bottom + Math.floor(index / columns) * height;
    card.widgets.push({
      id: 'jetson_' + id, kind: 'text', label, inlineLabelValue: true, fontSize: 12,
      value: { template, fallbackText: '—' }, fallbackText: '—',
      layout: { x, y, w, h: height },
    });
  }
  return settings;
}

module.exports = addJetsonMetrics;
if (require.main === module) {
  const fs = require('node:fs');
  const [, , file, robotType] = process.argv;
  if (!file) throw new Error('Usage: node patch-dashboard.cjs settings.json [robot_type]');
  const settings = JSON.parse(fs.readFileSync(file, 'utf8'));
  process.stdout.write(JSON.stringify(addJetsonMetrics(settings, robotType), null, 2) + '\n');
}
