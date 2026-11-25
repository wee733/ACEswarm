'use strict';

const API = '/ros2_bag_recorder/api';
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const state = {
  topics: [], encodedNames: new Set(), statCells: new Map(), selected: new Map(), rates: new Map(), presets: [], bags: [], logs: [],
  logKeys: new Set(), details: new Map(), openBag: null, panel: 'topics', status: null,
  busy: false, scanning: false, topicError: '', historyRequest: 0, streamOnline: false,
};

const statFields = ['actual_hz', 'written_count', 'dropped_count', 'queue_dropped_count', 'middleware_lost_count', 'written_bytes'];
function showText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}
function encodedVideo(topic) {
  return topic.type === 'foxglove_msgs/msg/CompressedVideo' || topic.type === 'ffmpeg_image_transport_msgs/msg/FFMPEGPacket' ||
    (topic.type === 'sensor_msgs/msg/CompressedImage' && /h264|h265|hevc/i.test(topic.name));
}
function selectedRate(name, requested) { return state.encodedNames.has(name) ? 0 : requested; }
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}
function icon(id) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const use = document.createElementNS(ns, 'use');
  use.setAttribute('href', `#${id}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.append(use);
  return svg;
}
function bytes(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  let amount = Number(value);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return `${number.format(amount)} ${units[unit]}`;
}
function duration(sec) {
  if (sec == null || !Number.isFinite(Number(sec))) return '—';
  const s = Math.max(0, Math.floor(sec));
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60]
    .map((n) => String(n).padStart(2, '0')).join(':');
}
function timestamp(raw, clockOnly = false) {
  if (raw == null || raw === '') return '—';
  let value = typeof raw === 'string' && /^-?\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : raw;
  if (typeof value === 'number') {
    if (value > 1e16) value /= 1e6;
    else if (value < 1e11) value *= 1000;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(raw);
  const time = date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  return clockOnly ? `${time}.${String(date.getMilliseconds()).padStart(3, '0')}` : `${date.toLocaleDateString('zh-CN')} ${time}`;
}
async function api(path, method = 'GET', data) {
  const opts = { method, cache: 'no-store' };
  if (data !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(data);
  }
  let response;
  try { response = await fetch(API + path, opts); }
  catch { throw new Error('无法连接录制服务，请检查连接后重试。'); }
  const result = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(result?.error || `请求失败（HTTP ${response.status}）`);
  return result;
}
let noticeTimer;
function notice(message, isError = false) {
  clearTimeout(noticeTimer);
  $('notice').querySelector('span').textContent = message;
  $('notice').dataset.error = isError;
  $('notice').hidden = false;
  if (!isError) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 5500);
}
$('notice').querySelector('button').onclick = () => { $('notice').hidden = true; };
function action(fn) {
  return async (...args) => {
    try { await fn(...args); } catch (error) { notice(error.message, true); }
  };
}
function connectIndicator(mode) {
  $('connection').dataset.state = mode;
  $('connection').querySelector('span').textContent = { online: '实时连接', offline: '正在重连', connecting: '连接中' }[mode];
}
function locked() { return state.busy || Boolean(state.status?.recording); }
function syncControls() {
  const disabled = locked();
  const recording = Boolean(state.status?.recording);
  for (const id of ['bag-name', 'notes', 'preset', 'format', 'compression', 'split-mb', 'split-seconds', 'save-rosout', 'select-all', 'select-video', 'batch-hz']) $(id).disabled = disabled;
  $('save-preset').disabled = disabled || !state.selected.size;
  $('delete-preset').disabled = disabled || !$('preset').value;
  $('apply-hz').disabled = $('clear').disabled = disabled || !state.selected.size;
  $('record').disabled = !state.status || state.busy || (!recording && !state.selected.size && !$('save-rosout').checked);
  $('record').dataset.active = recording;
  $('record').querySelector('span').textContent = state.busy ? '处理中…' : recording ? '停止录制' : '开始录制';
  $('selection-count').textContent = `已选 ${state.selected.size} 项`;
  $('scan').disabled = state.scanning;
}
const sessionFields = { 'bag-name': 'name', notes: 'notes', format: 'format', compression: 'compression', 'split-mb': 'split_mb', 'split-seconds': 'split_seconds', 'save-rosout': 'save_rosout' };
let formBeforeRecording;
function setStatus(next) {
  if (!next || typeof next.recording !== 'boolean') return;
  const previous = state.status;
  const sessionChanged = next.recording && (!previous?.recording || previous.name !== next.name);
  state.status = next;
  if (sessionChanged) {
    if (!formBeforeRecording) {
      const fields = {};
      for (const id of Object.keys(sessionFields)) fields[id] = $(id).type === 'checkbox' ? $(id).checked : $(id).value;
      formBeforeRecording = { fields, selected: state.selected, rates: state.rates };
    }
    // Show the actual session in every tab, then restore that tab's draft on stop.
    const names = next.requested_topics || Object.keys(next.topics || {});
    state.selected = new Map(names.map(name => [name, next.topics?.[name]?.max_hz || 0]));
    state.rates = new Map(Object.entries(next.topics || {}).map(([name, stats]) => [name, stats.max_hz || 0]));
    for (const [id, key] of Object.entries(sessionFields)) {
      if (next[key] == null) continue;
      if ($(id).type === 'checkbox') $(id).checked = next[key];
      else $(id).value = next[key];
    }
  } else if (!next.recording && formBeforeRecording) {
    for (const [id, value] of Object.entries(formBeforeRecording.fields)) {
      if ($(id).type === 'checkbox') $(id).checked = value;
      else $(id).value = value;
    }
    state.selected = formBeforeRecording.selected; state.rates = formBeforeRecording.rates;
    formBeforeRecording = null;
  }
  if (next.error && next.error !== previous?.error) notice(next.error, true);
  if (next.recording !== previous?.recording || sessionChanged) renderTopics();
  if (previous?.recording && !next.recording && state.panel === 'history') void refreshHistory();
  if (previous?.recording !== next.recording || sessionChanged) {
    $('state').dataset.active = next.recording;
    showText($('state').lastChild, next.recording ? '录制中' : '待机');
    syncControls();
  }
  showText($('elapsed'), duration(next.elapsed_seconds));
  showText($('size'), bytes(next.size_bytes));
  showText($('rate'), next.rate_bps == null ? '—' : `${number.format(next.rate_bps / 1024)} KB/s`);
  showText($('free'), bytes(next.free_bytes));
  showText($('queue'), next.buffer_bytes ? `${bytes(next.queue_bytes)} / ${bytes(next.buffer_bytes)}` : '—');
  showText($('queue-peak'), next.buffer_bytes ? `峰值 ${bytes(next.peak_queue_bytes)}` : '');
  updateStats();
}
function nonnegative(input, integer = false) {
  const v = input.valueAsNumber;
  if (!Number.isFinite(v) || v < 0 || (integer && !Number.isSafeInteger(v))) {
    input.reportValidity(); input.focus(); throw new Error(integer ? '请输入大于或等于 0 的整数。' : '请输入大于或等于 0 的频率。');
  }
  return v;
}
function selectedPayload() {
  // An input can still have focus when a button is activated using the keyboard.
  for (const input of $('topic-rows').querySelectorAll('input[type="number"]')) {
    if (state.selected.has(input.dataset.name)) state.selected.set(input.dataset.name, nonnegative(input));
  }
  return Array.from(state.selected, ([name, hz]) => ({ name, max_hz: selectedRate(name, hz) }));
}
$('record').onclick = action(async () => {
  const stopping = state.status?.recording;
  const data = stopping ? {} : {
    name: $('bag-name').value.trim(), notes: $('notes').value.trim(), topics: selectedPayload(),
    format: $('format').value, compression: $('compression').value,
    split_mb: nonnegative($('split-mb'), true), split_seconds: nonnegative($('split-seconds'), true), save_rosout: $('save-rosout').checked,
  };
  state.busy = true; syncControls();
  try {
    const status = await api(`/recorder/${stopping ? 'stop' : 'start'}`, 'POST', data);
    setStatus(status);
    if (stopping) notice('录制已停止，可在历史录包中查看文件。');
  } finally { state.busy = false; renderTopics(); syncControls(); }
});
$('save-rosout').onchange = syncControls;

function allTopics() {
  const rows = new Map(state.topics.map((topic) => [topic.name, topic]));
  for (const name of state.selected.keys()) {
    if (!rows.has(name)) rows.set(name, { name, type_available: false, missing: true });
  }
  return Array.from(rows.values()).sort((a, b) => a.name.localeCompare(b.name));
}
function visibleTopics() {
  const term = $('search').value.trim().toLowerCase();
  const ns = $('namespace').value;
  return allTopics().filter(t => (!term || `${t.name} ${t.type || ''}`.toLowerCase().includes(term)) &&
    (!ns || t.name.startsWith(ns + '/')) && (!$('selected-only').checked || state.selected.has(t.name)));
}
function updateNamespaces() {
  const previous = $('namespace').value;
  const prefixes = new Set();
  for (const topic of allTopics()) {
    const pieces = topic.name.split('/').filter(Boolean);
    for (let n = 1; n < pieces.length; n++) prefixes.add('/' + pieces.slice(0, n).join('/'));
  }
  $('namespace').replaceChildren(new Option('全部命名空间', ''), ...Array.from(prefixes).sort().map(p => new Option(p, p)));
  if (prefixes.has(previous)) $('namespace').value = previous;
}
function renderTopics() {
  const fragment = document.createDocumentFragment();
  const rows = visibleTopics();
  state.statCells.clear();
  for (const topic of rows) {
    const selected = state.selected.has(topic.name);
    const row = el('tr', undefined, selected ? 'selected' : '');
    row.dataset.topic = topic.name;
    const check = el('input'); check.type = 'checkbox'; check.checked = selected;
    check.disabled = locked() || (!selected && !topic.type_available); check.setAttribute('aria-label', `选择 ${topic.name}`);
    const first = el('td'); first.append(check);
    const info = el('td'); info.append(el('span', topic.name, 'topic-name'), el('span', topic.type || '—', 'type-name'));
    if (!topic.type_available) info.append(el('span', topic.missing ? '当前未发现此话题' : '消息类型不可用，请在参数中心配置 overlay', 'warn-text'));
    const qos = el('td');
    qos.append(el('div', topic.reliability || '—', 'qos'), el('div', topic.durability || '—', 'qos'));
    if (Array.isArray(topic.publisher_qos)) qos.title = topic.publisher_qos.map(q => [q.reliability, q.durability].filter(Boolean).join(' / ')).join('\n');
    const frequency = el('input'); frequency.type = 'number'; frequency.min = '0'; frequency.step = 'any'; frequency.required = true;
    const encoded = state.encodedNames.has(topic.name);
    frequency.value = selectedRate(topic.name, state.rates.get(topic.name) ?? 0);
    frequency.disabled = locked() || !topic.type_available || encoded;
    frequency.dataset.name = topic.name; frequency.setAttribute('aria-label', `${topic.name} 上限 Hz`);
    frequency.oninput = () => {
      if (frequency.validity.valid) {
        state.rates.set(topic.name, frequency.valueAsNumber);
        if (selected) state.selected.set(topic.name, frequency.valueAsNumber);
      }
    };
    check.onchange = action(() => {
      if (check.checked) {
        try { state.selected.set(topic.name, nonnegative(frequency)); }
        catch (error) { check.checked = false; throw error; }
      } else state.selected.delete(topic.name);
      renderTopics();
    });
    const freqCell = el('td'); freqCell.append(frequency);
    if (encoded) {
      freqCell.title = '编码视频需要连续的数据包才能解码，录制时保持全速。';
      freqCell.append(el('span', '编码流全速', 'encoded-hint'));
    }
    row.append(first, info, el('td', `${topic.publishers ?? '—'} / ${topic.subscribers ?? '—'}`, 'mono'), qos, freqCell);
    const cells = statFields.map(field => {
      const cell = el('td', '—', 'num'); cell.dataset.stat = field; row.append(cell); return cell;
    });
    state.statCells.set(topic.name, { cells, values: [] });
    fragment.append(row);
  }
  $('topic-rows').replaceChildren(fragment);
  $('topic-count').textContent = `${state.topics.length} 个`;
  $('topic-empty').hidden = rows.length > 0;
  $('topic-empty').textContent = state.topicError || (state.scanning ? '正在扫描 ROS 2 话题…' : state.topics.length ? '没有匹配的话题。' : '尚未发现 ROS 2 话题。');
  updateStats(); syncControls();
}
function updateStats() {
  if (state.panel !== 'topics') return;
  const stats = state.status?.topics || {};
  for (const [name, row] of state.statCells) {
    const values = stats[name];
    for (let i = 0; i < statFields.length; i++) {
      const value = values?.[statFields[i]];
      if (value === row.values[i]) continue;
      row.values[i] = value;
      showText(row.cells[i], value == null ? '—' : number.format(value));
    }
  }
}
async function scan() {
  state.scanning = true; state.topicError = ''; syncControls();
  try {
    state.topics = (await api('/topics')).topics;
    state.encodedNames = new Set(state.topics.filter(encodedVideo).map(t => t.name));
    for (const name of state.encodedNames) {
      state.rates.set(name, 0);
      if (state.selected.has(name)) state.selected.set(name, 0);
    }
    updateNamespaces();
  } catch (error) { state.topicError = error.message; notice(error.message, true); }
  finally { state.scanning = false; renderTopics(); }
}
$('scan').onclick = scan;
$('search').oninput = $('namespace').onchange = $('selected-only').onchange = renderTopics;
$('select-all').onclick = () => {
  for (const t of visibleTopics()) if (t.type_available) state.selected.set(t.name, state.rates.get(t.name) || 0);
  renderTopics();
};
$('select-video').onclick = () => {
  const ns = $('namespace').value;
  const publicTopics = state.topics.filter(t => t.type_available && !t.name.split('/').some(part => part.startsWith('_')));
  const cameras = publicTopics.filter(t => t.type === 'sensor_msgs/msg/CameraInfo')
    .map(t => ({ name: t.name, prefix: t.name.slice(0, t.name.lastIndexOf('/') + 1) }))
    .sort((a, b) => b.prefix.length - a.prefix.length);
  const videos = publicTopics.filter(t => state.encodedNames.has(t.name) && (!ns || t.name.startsWith(ns + '/')));
  const intrinsics = new Set();
  for (const topic of videos) {
    state.selected.set(topic.name, 0); state.rates.set(topic.name, 0);
    const parent = topic.name.slice(0, topic.name.lastIndexOf('/') + 1);
    const match = cameras.find(c => topic.name.startsWith(c.prefix) && (c.prefix !== '/' || parent === '/'));
    if (match) {
      for (const camera of cameras.filter(c => c.prefix === match.prefix)) {
        intrinsics.add(camera.name); state.selected.set(camera.name, state.rates.get(camera.name) || 0);
      }
    }
  }
  renderTopics();
  notice(videos.length ? `已选择 ${videos.length} 路编码视频及 ${intrinsics.size} 个内参话题。` : '当前命名空间没有发现可录制的编码视频话题。');
};
$('clear').onclick = () => { state.selected.clear(); renderTopics(); };
$('apply-hz').onclick = action(() => {
  const value = nonnegative($('batch-hz'));
  for (const name of state.selected.keys()) {
    const hz = selectedRate(name, value); state.selected.set(name, hz); state.rates.set(name, hz);
  }
  renderTopics();
});

async function ask({ title, text, label, initial, danger = false }) {
  const dialog = $('dialog');
  $('dialog-title').textContent = title;
  $('dialog-text').textContent = text || '';
  $('dialog-field').hidden = initial === undefined;
  $('dialog-input').required = initial !== undefined;
  $('dialog-input').value = initial || '';
  $('dialog-submit').textContent = label || '确认';
  $('dialog-submit').className = danger ? 'danger' : 'action-primary';
  dialog.returnValue = 'cancel'; dialog.showModal();
  if (initial !== undefined) { $('dialog-input').focus(); $('dialog-input').select(); }
  else dialog.querySelector('button[value="cancel"]').focus();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok' ? (initial === undefined ? true : $('dialog-input').value.trim()) : null), { once: true }));
}
async function loadPresets(selected = '') {
  state.presets = (await api('/presets')).presets;
  $('preset').replaceChildren(new Option('选择预设', ''), ...state.presets.map(p => new Option(p.name, p.name)));
  $('preset').value = selected; syncControls();
}
$('preset').onchange = () => {
  const preset = state.presets.find(p => p.name === $('preset').value);
  if (preset) {
    state.selected.clear();
    for (const t of preset.topics) {
      const hz = selectedRate(t.name, t.max_hz); state.selected.set(t.name, hz); state.rates.set(t.name, hz);
    }
    updateNamespaces(); renderTopics();
  }
  syncControls();
};
$('save-preset').onclick = action(async () => {
  const topics = selectedPayload();
  const name = await ask({ title: '保存话题预设', text: `保存 ${topics.length} 个话题及各自的限频。`, initial: $('preset').value, label: '保存' });
  if (!name) return;
  if (state.presets.some(p => p.name === name) && !await ask({ title: '覆盖预设', text: `用当前选择替换“${name}”的配置？`, label: '覆盖' })) return;
  await api('/presets', 'POST', { name, topics }); await loadPresets(name); notice('话题预设已保存。');
});
$('delete-preset').onclick = action(async () => {
  const name = $('preset').value;
  if (name && await ask({ title: '删除预设', text: `确认删除“${name}”？`, label: '删除', danger: true })) {
    await api(`/presets/${encodeURIComponent(name)}`, 'DELETE'); await loadPresets(); notice('话题预设已删除。');
  }
});

function levelName(raw) {
  return ({ 10: 'DEBUG', 20: 'INFO', 30: 'WARN', 40: 'ERROR', 50: 'FATAL', WARNING: 'WARN' })[raw] || String(raw || '—').toUpperCase();
}
let logPaint = null;
function addLog(entry) {
  const key = JSON.stringify([entry.id, entry.stamp, entry.node, entry.message]);
  if (state.logKeys.has(key)) return;
  state.logs.push({ key, entry }); state.logKeys.add(key);
  if (state.logs.length > 500) state.logKeys.delete(state.logs.shift().key);
  if (state.panel === 'logs' && !logPaint) logPaint = setTimeout(() => { logPaint = null; renderLogs(); }, 200);
}
function renderLogs() {
  const node = $('node').value.trim().toLowerCase();
  const level = $('level').value;
  const visible = state.logs.filter(({ entry }) => (!level || levelName(entry.level) === level) && (!node || String(entry.node || '').toLowerCase().includes(node)));
  const fragment = document.createDocumentFragment();
  for (const { entry } of visible.reverse()) {
    const row = el('tr'); const lev = levelName(entry.level);
    row.append(el('td', timestamp(entry.stamp, true), 'clock'), el('td', lev, 'level ' + lev), el('td', entry.node, 'wrap-any'), el('td', entry.message, 'log-message'));
    fragment.append(row);
  }
  $('log-rows').replaceChildren(fragment);
  $('log-count').textContent = `${visible.length} / ${state.logs.length} 条`;
  $('log-empty').hidden = Boolean(visible.length);
  $('log-empty').textContent = state.logs.length ? '没有匹配的日志。' : '等待 /rosout 日志…';
}
$('node').oninput = $('level').onchange = renderLogs;

function bagPath(name) { return `/bags/${encodeURIComponent(name)}`; }
function downloadLink(name, filename) {
  const a = el('a', undefined, 'button-link row-action');
  a.href = `${API}${bagPath(name)}/download${filename ? '?file=' + encodeURIComponent(filename) : ''}`;
  a.download = filename || `${name}.tar.gz`;
  a.append(icon('download-icon'), document.createTextNode(filename || '下载整包'));
  return a;
}
function bagAction(label, handler, danger = false) {
  const button = el('button', label, danger ? 'row-action danger' : 'row-action'); button.type = 'button'; button.onclick = action(handler); return button;
}
async function copyPath(path) {
  try {
    if (!navigator.clipboard) throw new Error();
    await navigator.clipboard.writeText(path);
  } catch {
    const input = el('textarea'); input.value = path; input.style.position = 'fixed'; input.style.left = '-10000px';
    document.body.append(input); input.select(); const ok = document.execCommand('copy'); input.remove();
    if (!ok) throw new Error(`请手动复制录包路径：${path}`);
  }
  notice('录包路径已复制。');
}
function detailContent(bag) {
  const box = el('div', undefined, 'detail-grid');
  const path = el('div', undefined, 'detail-path');
  path.append(el('span', bag.path || '—', 'mono'));
  if (bag.path) path.append(bagAction('复制路径', () => copyPath(bag.path)));
  box.append(path);
  const topics = el('div'); topics.append(el('h3', '各话题帧数'));
  for (const t of bag.topics || []) {
    const line = el('div', undefined, 'detail-line'); const info = el('div');
    info.append(el('span', t.name, 'topic-name'), el('span', t.type, 'type-name'));
    line.append(info, el('span', number.format(t.count || 0), 'mono')); topics.append(line);
  }
  if (!bag.topics?.length) topics.append(el('p', '无话题记录', 'muted'));
  const downloads = el('div'); downloads.append(el('h3', '录包文件')); const files = el('div', undefined, 'file-list');
  for (const file of bag.files || []) files.append(downloadLink(bag.name, file));
  downloads.append(files); box.append(topics, downloads);
  return box;
}
function renderHistory() {
  const fragment = document.createDocumentFragment();
  for (const bag of state.bags) {
    const row = el('tr'); row.dataset.bag = bag.name;
    const title = el('td'); title.append(el('div', bag.name, 'history-name wrap-any'));
    if (bag.note) title.append(el('div', bag.note, 'muted wrap-any'));
    const actions = el('div', undefined, 'history-actions');
    const toggle = bagAction(state.openBag === bag.name ? '收起' : '详情', async () => {
      if (state.openBag === bag.name) { state.openBag = null; renderHistory(); return; }
      state.openBag = bag.name; renderHistory();
      try { state.details.set(bag.name, await api(bagPath(bag.name))); }
      catch (error) { if (state.openBag === bag.name) state.openBag = null; throw error; }
      finally { renderHistory(); }
    });
    toggle.setAttribute('aria-expanded', state.openBag === bag.name);
    actions.append(toggle, downloadLink(bag.name), bagAction('删除', async () => {
      if (await ask({ title: '删除录包', text: `将永久删除“${bag.name}”及其所有文件。`, label: '删除录包', danger: true })) {
        await api(bagPath(bag.name), 'DELETE'); await refreshHistory(); notice('录包已删除。');
      }
    }, true));
    const actionsCell = el('td'); actionsCell.append(actions);
    row.append(title, el('td', timestamp(bag.start_time), 'clock'), el('td', duration(bag.duration_sec), 'mono'), el('td', bytes(bag.size_bytes), 'mono'), el('td', bag.topic_count ?? '—', 'mono'), el('td', number.format(bag.message_count || 0), 'mono'), actionsCell);
    fragment.append(row);
    if (state.openBag === bag.name) {
      const detailRow = el('tr', undefined, 'history-detail'); const cell = el('td'); cell.colSpan = 7;
      const detail = state.details.get(bag.name); cell.append(detail ? detailContent(detail) : el('p', '正在读取录包详情…', 'muted'));
      detailRow.append(cell); fragment.append(detailRow);
    }
  }
  $('history-rows').replaceChildren(fragment);
  $('bag-count').textContent = `${state.bags.length} 个`;
  $('history-empty').hidden = Boolean(state.bags.length); $('history-empty').textContent = '暂无历史录包。';
}
async function refreshHistory() {
  const request = ++state.historyRequest;
  $('refresh-history').disabled = true;
  try {
    const result = await api('/bags');
    if (request !== state.historyRequest) return;
    state.bags = result.bags; state.openBag = null; state.details.clear(); renderHistory();
  } catch (error) {
    if (request !== state.historyRequest) return;
    $('history-empty').hidden = false; $('history-empty').textContent = error.message; notice(error.message, true);
  } finally { if (state.historyRequest === request) $('refresh-history').disabled = false; }
}
$('refresh-history').onclick = refreshHistory;

const panels = ['topics', 'logs', 'history'];
function activate(panel) {
  state.panel = panel;
  for (const p of panels) {
    const active = p === panel;
    $(`tab-${p}`).setAttribute('aria-selected', active); $(`tab-${p}`).tabIndex = active ? 0 : -1;
    $(`panel-${p}`).hidden = !active;
  }
  if (panel === 'topics') updateStats();
  if (panel === 'logs') renderLogs();
  if (panel === 'history') void refreshHistory();
}
for (const [index, panel] of panels.entries()) {
  const tab = $(`tab-${panel}`); tab.onclick = () => activate(panel);
  tab.onkeydown = (event) => {
    const target = { ArrowRight: (index + 1) % 3, ArrowLeft: (index + 2) % 3, Home: 0, End: 2 }[event.key];
    if (target !== undefined) { event.preventDefault(); activate(panels[target]); $(`tab-${panels[target]}`).focus(); }
  };
}
function startEvents() {
  const stream = new EventSource(`${API}/recorder/events`);
  stream.onopen = () => { state.streamOnline = true; connectIndicator('online'); };
  stream.onerror = () => { state.streamOnline = false; connectIndicator('offline'); };
  stream.addEventListener('status', e => setStatus(JSON.parse(e.data)));
  stream.addEventListener('log', e => addLog(JSON.parse(e.data)));
  return stream;
}
async function initialStatus() {
  try { setStatus(await api('/recorder/status')); }
  catch (error) { if (!state.streamOnline) { connectIndicator('offline'); notice(error.message, true); } }
}
void scan(); void action(loadPresets)();
let stream;
void initialStatus().finally(() => { stream = startEvents(); });
// EventSource reconnects by itself. While it reconnects, the status endpoint keeps the stop control current.
const poll = setInterval(() => { if (!state.streamOnline && !state.busy) void initialStatus(); }, 5000);
window.addEventListener('pagehide', () => { stream?.close(); clearInterval(poll); });
