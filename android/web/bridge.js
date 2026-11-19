import { resolveAppearance } from '../../electron/services/appearance.js';
import { normalizePageUrl, sameOrigin } from './url.mjs';

// This bridge is installed only in the bundled shell. Native guest WebViews do
// not share it. Package IDs belong to a virtual origin used by upstream favorites.
const host = window.AceAndroid;
const packageOrigin = 'https://apps.aceswarm.invalid';
const stationId = 'com.aivuda.h16.station';
const consoleEntry = packageOrigin + '/' + stationId;
const storeEntry = 'https://workbench.aceswarm.invalid/store';
const defaultPins = ['org.mavlink.qgroundcontrol', 'com.nomachine.nxplayer'];
const appearanceSubscribers = [];
let catalog = [];
let desktopReady = false;
let recent = read('recent', []);
let clearingBrowserData = false;
let pendingClearBrowserData = false;
let lastSavedDesktop = null;

function read(key, fallback) {
  try {
    const stored = localStorage.getItem('android.' + key);
    if (key === 'desktop') lastSavedDesktop = stored;
    if (key === 'appearance' || key === 'store') {
      const value = host.getSetting(key);
      if (value !== null && value !== '') return JSON.parse(value);
      if (stored !== null) host.setSetting(key, stored);
    }
    return JSON.parse(stored) ?? fallback;
  }
  catch (_) { return fallback; }
}
function write(key, value) {
  if (key === 'appearance' || key === 'store') {
    host.setSetting(key, JSON.stringify(value));
  } else if (!clearingBrowserData) {
    const serialized = JSON.stringify(value);
    if (key === 'desktop') {
      if (serialized === lastSavedDesktop) return;
      lastSavedDesktop = serialized;
    }
    localStorage.setItem('android.' + key, serialized);
  }
}
function appearance() {
  return resolveAppearance(read('appearance', {}), host.locale(),
    window.matchMedia('(prefers-color-scheme: dark)').matches);
}
function refreshCatalog() {
  catalog = JSON.parse(host.apps()).map(app => ({
    id: app.id, title: app.title, url: packageOrigin + '/' + encodeURIComponent(app.id), favicon: app.icon,
  }));
  return catalog;
}
function launcherCatalog() {
  return catalog.filter(entry => entry.id !== stationId);
}
function syncHomeButton() {
  const button = document.getElementById('open-home');
  if (button) button.hidden = !catalog.some(entry => entry.id === stationId);
}

function currentEntries(entries, excludeBuiltins = false) {
  if (!Array.isArray(entries)) return [];
  return entries
    .filter(entry => entry && typeof entry.url === 'string'
      && (!excludeBuiltins || (entry.url !== consoleEntry && entry.url !== storeEntry)))
    .map(entry => entry.url.startsWith(packageOrigin + '/')
      ? catalog.find(app => app.url === entry.url) : entry)
    .filter(Boolean);
}

function syncCatalog() {
  refreshCatalog();
  installedApplications = launcherCatalog();
  favorites = currentEntries(favorites, true);
  recent = currentEntries(recent);
  syncHomeButton();
  write('recent', recent);
  renderDesktop();
  if (document.getElementById('application-launcher').open) renderApplicationLauncher();
  writeShellState();
}

function builtinTitle(title) {
  if (appearance().resolvedLanguage !== 'zh-CN') return title;
  return title === 'Console' ? '控制台' : '商店管理';
}

function savedStoreUrl() {
  return normalizePageUrl(read('store', ''));
}

function remember(entry) {
  recent = [entry, ...recent.filter(other => other.url !== entry.url)].slice(0, 12);
  write('recent', recent);
  renderDesktop();
}

function openOnDevice(url) {
  if (url === storeEntry) {
    const address = savedStoreUrl();
    if (!address) return editStore();
    host.openStore(address);
    remember({ title: builtinTitle('AppStore Admin'), url: storeEntry });
    return;
  }
  const local = catalog.find(entry => entry.url === url);
  if (local) {
    host.launch(local.id);
    remember({ title: local.title, url, favicon: local.favicon });
  } else if (url === consoleEntry) {
    showOpenPageDialog();
  } else {
    const normalized = normalizePageUrl(url);
    if (normalized) {
      const store = savedStoreUrl();
      if (store && sameOrigin(normalized, store)) host.openStore(normalized);
      else host.openPage(normalized);
      remember({ title: new URL(normalized).host, url: normalized });
    } else {
      refreshCatalog();
      openApplicationLauncher();
    }
  }
}

function renderDeviceTasks(container) {
  for (const entry of recent) {
    const title = entry.url === storeEntry ? builtinTitle('AppStore Admin') : entry.title;
    const builtin = entry.url === consoleEntry || entry.url === storeEntry;
    const button = document.createElement('button');
    button.className = 'dock-item window-task';
    button.title = title;
    button.setAttribute('aria-label', title);
    const label = document.createElement('span');
    label.className = 'dock-label';
    label.textContent = title;
    button.append(applicationIcon(entry), label);
    button.onclick = () => openOnDevice(entry.url);
    const key = desktopState.appKey(entry.url);
    button.oncontextmenu = event => openDockMenu(event, { ...entry, title, key, builtin,
      pinned: builtin || favorites.some(item => desktopState.appKey(item.url) === key), windows: [] });
    container.append(button);
  }
}

function editStore() {
  setToolsMenuOpen(false);
  const dialog = document.getElementById('android-store-dialog');
  dialog.querySelector('input').value = savedStoreUrl();
  dialog.showModal();
  dialog.querySelector('input').focus();
}

function installAndroidControls() {
  const text = (en, zh) => appearance().resolvedLanguage === 'zh-CN' ? zh : en;
  const storeSettings = document.createElement('button');
  storeSettings.id = 'android-store-settings';
  storeSettings.setAttribute('role', 'menuitem');
  const storeSettingsLabel = document.createElement('span');
  storeSettings.append(desktopIcon('store'), storeSettingsLabel);
  const quit = document.getElementById('system-quit');
  quit.parentNode.insertBefore(storeSettings, quit);
  storeSettings.onclick = editStore;
  const dialog = document.createElement('dialog');
  dialog.id = 'android-store-dialog';
  dialog.innerHTML = '<form><div class="dialog-heading"><h2></h2><button type="button" class="icon-button">×</button></div>'
    + '<label for="android-store-url"></label><input type="text" id="android-store-url" placeholder="http:// or https://" required autocomplete="off">'
    + '<div class="dialog-actions"><button type="submit" class="primary"></button></div></form>';
  document.body.append(dialog);
  dialog.querySelector('button[type="button"]').onclick = () => dialog.close();
  dialog.querySelector('form').onsubmit = event => {
    event.preventDefault();
    const input = dialog.querySelector('input');
    const url = normalizePageUrl(input.value);
    if (!url) {
      input.setCustomValidity(text('Enter an HTTP or HTTPS server address', '请输入 HTTP 或 HTTPS 服务器地址'));
      input.reportValidity();
      return;
    }
    write('store', url);
    dialog.close();
    openOnDevice(storeEntry);
  };
  dialog.querySelector('input').oninput = event => event.target.setCustomValidity('');
  function translate() {
    const title = text('AppStore address', '应用商店地址');
    storeSettingsLabel.textContent = title;
    document.getElementById('open-home').textContent = builtinTitle('Console');
    document.getElementById('open-store').textContent = builtinTitle('AppStore Admin');
    if (desktopReady) {
      renderDesktop();
      if (document.getElementById('application-launcher').open) renderApplicationLauncher();
    }
    dialog.querySelector('h2').textContent = title;
    dialog.querySelector('label').textContent = text('Server URL', '服务器网址');
    dialog.querySelector('button[type=submit]').textContent = text('Save and open', '保存并打开');
    dialog.querySelector('button[type=button]').setAttribute('aria-label', text('Close', '关闭'));
  }
  translate();
  appearanceSubscribers.push(translate);
  for (const id of ['tools-record', 'tools-fps', 'show-desktop', 'system-desktop']) {
    const node = document.getElementById(id);
    if (node) node.hidden = true;
  }
  syncHomeButton();
  refreshDesktopIcons();
  document.getElementById('downloads-button').onclick = () => host.downloads();
}

const api = {
  desktopReady: () => { desktopReady = true; syncHomeButton(); },
  builtinTitle, openOnDevice, renderDeviceTasks,
  getAppearance: appearance,
  setAppearance(value) {
    write('appearance', { theme: value.theme, language: value.language });
    const next = appearance();
    for (const notify of appearanceSubscribers) notify(next);
    return Promise.resolve(next);
  },
  onAppearance: callback => appearanceSubscribers.push(callback),
  getInstalledApplications: async () => { refreshCatalog(); return launcherCatalog(); },
  getRunningApplications: async () => [],
  getStartup: async () => {
    refreshCatalog();
    syncHomeButton();
    const firstState = { favorites: defaultPins.map(id => catalog.find(entry => entry.id === id)).filter(Boolean) };
    const savedState = read('desktop', firstState);
    savedState.favorites = currentEntries(savedState.favorites || [], true);
    recent = currentEntries(recent);
    return { defaultUrl: consoleEntry, storeUrl: storeEntry, savedState };
  },
  clearBrowserData: async () => {
    if (clearingBrowserData || pendingClearBrowserData) return { ok: false };
    pendingClearBrowserData = true;
    const zh = appearance().resolvedLanguage === 'zh-CN';
    const message = zh
      ? '清除 ACEswarm 浏览数据？将退出网页账号并清除收藏、最近应用与网页缓存。'
      : 'Clear ACEswarm browser data? This signs you out of web pages and clears favorites, recent apps and cached pages.';
    const confirmLabel = zh ? '清除' : 'Clear';
    const cancelLabel = zh ? '取消' : 'Cancel';
    return new Promise(resolve => {
      window.aceAndroidDataCleared = cleared => {
        delete window.aceAndroidDataCleared;
        pendingClearBrowserData = false;
        if (!cleared) { resolve({ ok: false }); return; }
        clearingBrowserData = true;
        localStorage.clear();
        sessionStorage.clear();
        resolve({ ok: true });
      };
      host.clearBrowserData(message, confirmLabel, cancelLabel);
    });
  },
  saveShellState: async state => write('desktop', state),
  desktopCommand: async action => {
    if (action === 'fullscreen') host.fullscreen();
    else if (action === 'quit') host.quit();
  },
  authorizeUrl: async url => ({
    ok: Boolean(normalizePageUrl(url)),
    error: appearance().resolvedLanguage === 'zh-CN' ? '请输入有效的 HTTP 或 HTTPS 地址。' : 'Use an HTTP or HTTPS address.',
  }),
};
for (const callback of [
  'onCloseCurrentTab', 'onClearBrowserData', 'onHideBrowserChrome', 'onHidePerformanceOverlay', 'onNewTab',
  'onOpenUrlInNewTab', 'onReloadCurrentTab', 'onResetZoom', 'onFindInPage', 'onShowPerformanceOverlay',
  'onShowBrowserChrome', 'onToggleScreenRecordBar', 'onToggleBrowserChrome', 'onToggleDevtools',
  'onTogglePerformanceOverlay', 'onZoomIn', 'onZoomOut', 'onRecordingError',
]) api[callback] = () => {};
window.aivudaShell = api;
window.aceAndroidResume = () => {
  if (!desktopReady) return;
  syncCatalog();
  const value = appearance();
  for (const notify of appearanceSubscribers) notify(value);
};
window.aceAndroidBack = () => {
  const dialog = document.querySelector('dialog[open]');
  if (dialog) { dialog.close(); return true; }
  if (!document.getElementById('tools-menu').hidden || !document.getElementById('applications-menu').hidden ||
      !document.getElementById('dock-menu').hidden) {
    document.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  }
  return false;
};
window.addEventListener('DOMContentLoaded', installAndroidControls);
