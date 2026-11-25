// Upgrade the two legacy local overrides once, preserving subsequent edits in the card editor.
function migrateBatteryFreshness(settings, storage, key) {
  const marker = key + '.battery_timeout_v1';
  if (storage.getItem(marker)) return settings;
  let changed = false;
  const templates = settings?.templates || settings || {};
  for (const name of ['uxrce_mocap', 'uxrce_rotor']) {
    const battery = templates[name]?.sources?.find(source => source.id === 'source_battery');
    if (battery?.freshnessMs === 3000) {
      battery.freshnessMs = 10000;
      changed = true;
    }
  }
  try {
    if (changed) storage.setItem(key, JSON.stringify(settings));
    storage.setItem(marker, '1');
  } catch {
    // The current view can still use the upgraded settings when storage is full.
  }
  return settings;
}
