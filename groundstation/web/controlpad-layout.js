// Upgrade the shipped uXRCE layout while retaining locally edited widget positions.
function updateControlpadLayout(saved, defaults) {
  if (!saved || saved.id !== 'uxrce' || defaults.id !== 'uxrce') return saved || defaults;
  const widgets = saved.widgets.map(widget => {
    const preset = defaults.widgets.find(item =>
      item.id === widget.id && item.keySuffix === widget.keySuffix && item.requestType === widget.requestType);
    if (!preset) return widget;
    if (widget.keySuffix === '/control/arm' && widget.payloadTemplate?.trim() === '{}') {
      return {...widget, confirm: true, payloadTemplate: preset.payloadTemplate};
    }
    if (widget.keySuffix === '/control/set_mode' && widget.type === 'select-button') {
      const options = widget.selectOptions || [];
      const extra = preset.selectOptions.filter(option =>
        option.value.startsWith('EXTRA') || option.value.startsWith('EXTERNAL'));
      return {...widget, selectOptions: [...options, ...extra.filter(option =>
        !options.some(existing => existing.value === option.value))],
        zenohOptions: {...widget.zenohOptions, timeoutMs: Math.max(widget.zenohOptions?.timeoutMs || 0, 6000)}};
    }
    return widget;
  });
  const register = defaults.widgets.find(widget => widget.id === 'register_extra');
  if (register && !widgets.some(widget => widget.id === register.id || widget.keySuffix === register.keySuffix)) {
    const bottom = Math.max(0, ...widgets.map(widget => widget.gridConfig.y + widget.gridConfig.h));
    widgets.push({...register, gridConfig: {...register.gridConfig, y: bottom}});
  }
  const goto = defaults.widgets.find(widget => widget.id === 'goto');
  if (goto && !widgets.some(widget => widget.id === goto.id || widget.keySuffix === goto.keySuffix)) {
    const grid = {...goto.gridConfig};
    const cols = saved.cols || defaults.cols || 24;
    grid.w = Math.min(grid.w, cols);
    grid.x = Math.min(grid.x, cols - grid.w);
    const occupied = widgets.map(widget => widget.gridConfig);
    if (occupied.some(p => grid.x < p.x + p.w && p.x < grid.x + grid.w &&
                           grid.y < p.y + p.h && p.y < grid.y + grid.h)) {
      grid.y = Math.max(...occupied.map(p => p.y + p.h));
    }
    widgets.push({...goto, gridConfig: grid});
  }
  return {...saved, widgets};
}
