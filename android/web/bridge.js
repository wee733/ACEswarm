import { resolveAppearance } from '../../electron/services/appearance.js';

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

function read(key, fallback) {
  try {
    const stored = localStorage.getItem('android.' + key);
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
    localStorage.setItem('android.' + key, JSON.stringify(value));
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

function currentEntries(entries) {
  return entries.map(entry => entry.url.startsWith(packageOrigin + '/')
    ? catalog.find(app => app.url === entry.url) : entry).filter(Boolean);
}

function syncCatalog() {
  installedApplications = refreshCatalog();
  favorites = currentEntries(favorites);
  recent = currentEntries(recent);
  write('recent', recent);
  renderDesktop();
  if (document.getElementById('application-launcher').open) renderApplicationLauncher();
  writeShellState();
}

function builtinTitle(title) {
  if (appearance().resolvedLanguage !== 'zh-CN') return title;
  return title === 'Console' ? '控制台' : '商店管理';
}

function validPage(text) {
  try {
    const parsed = new URL(text);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && Boolean(parsed.hostname)
      && !parsed.username && !parsed.password && parsed.port !== '0';
  } catch (_) { return false; }
}

function remember(entry) {
  recent = [entry, ...recent.filter(other => other.url !== entry.url)].slice(0, 12);
  write('recent', recent);
  renderDesktop();
}

function openOnDevice(url) {
  if (url === storeEntry) {
    const address = read('store', '');
    if (!address) return editStore();
    return openOnDevice(address);
  }
  const local = catalog.find(entry => entry.url === url);
  if (local) {
    host.launch(local.id);
    remember({ title: local.title, url, favicon: local.favicon });
  } else if (url === consoleEntry) {
    showOpenPageDialog();
  } else if (validPage(url) && new URL(url).origin !== packageOrigin) {
    if (url === read('store', '')) host.openStore(url);
    else host.openPage(url);
    remember({ title: new URL(url).host, url });
  } else {
    refreshCatalog();
    openApplicationLauncher();
  }
}

function renderDeviceTasks(container) {
  for (const entry of recent) {
    const button = document.createElement('button');
    button.className = 'dock-item window-task';
    button.title = entry.title;
    button.setAttribute('aria-label', entry.title);
    const label = document.createElement('span');
    label.className = 'dock-label';
    label.textContent = entry.title;
    button.append(applicationIcon(entry), label);
    button.onclick = () => openOnDevice(entry.url);
    button.oncontextmenu = event => openDockMenu(event, { ...entry, key: desktopState.appKey(entry.url),
      pinned: favorites.some(item => item.url === entry.url), windows: [] });
    container.append(button);
  }
}

function editStore() {
  setToolsMenuOpen(false);
  const dialog = document.getElementById('android-store-dialog');
  dialog.querySelector('input').value = read('store', '');
  dialog.showModal();
  dialog.querySelector('input').focus();
}

function installAndroidControls() {
  const text = (en, zh) => appearance().resolvedLanguage === 'zh-CN' ? zh : en;
  const storeSettings = document.createElement('button');
  storeSettings.id = 'android-store-settings';
  storeSettings.setAttribute('role', 'menuitem');
  const quit = document.getElementById('system-quit');
  quit.parentNode.insertBefore(storeSettings, quit);
  storeSettings.onclick = editStore;
  const dialog = document.createElement('dialog');
  dialog.id = 'android-store-dialog';
  dialog.innerHTML = '<form><div class="dialog-heading"><h2></h2><button type="button" class="icon-button">×</button></div>'
    + '<label for="android-store-url"></label><input type="url" id="android-store-url" placeholder="https://" required>'
    + '<div class="dialog-actions"><button type="submit" class="primary"></button></div></form>';
  document.body.append(dialog);
  dialog.querySelector('button[type="button"]').onclick = () => dialog.close();
  dialog.querySelector('form').onsubmit = event => {
    event.preventDefault();
    const input = dialog.querySelector('input');
    const url = input.value.trim();
    if (!validPage(url) || ['workbench.aceswarm.invalid', 'apps.aceswarm.invalid'].includes(new URL(url).hostname)) {
      input.setCustomValidity(text('Enter an HTTP or HTTPS server address', '请输入 HTTP 或 HTTPS 服务器地址'));
      input.reportValidity();
      return;
    }
    write('store', url);
    dialog.close();
    openOnDevice(url);
  };
  dialog.querySelector('input').oninput = event => event.target.setCustomValidity('');
  function translate() {
    const title = text('AppStore address', '应用商店地址');
    storeSettings.textContent = title;
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
  for (const id of ['tools-record', 'tools-fps']) document.getElementById(id).hidden = true;
  document.getElementById('downloads-button').onclick = () => host.downloads();
}

const api = {
  desktopReady: () => { desktopReady = true; },
  builtinTitle, openOnDevice, renderDeviceTasks,
  getAppearance: appearance,
  setAppearance(value) {
    write('appearance', { theme: value.theme, language: value.language });
    const next = appearance();
    for (const notify of appearanceSubscribers) notify(next);
    return Promise.resolve(next);
  },
  onAppearance: callback => appearanceSubscribers.push(callback),
  getInstalledApplications: async () => refreshCatalog(),
  getRunningApplications: async () => [],
  getStartup: async () => {
    refreshCatalog();
    const firstState = { favorites: defaultPins.map(id => catalog.find(entry => entry.id === id)).filter(Boolean) };
    const savedState = read('desktop', firstState);
    savedState.favorites = currentEntries(savedState.favorites || []);
    recent = currentEntries(recent);
    return { defaultUrl: consoleEntry, storeUrl: storeEntry, savedState };
  },
  clearBrowserData: async () => {
    if (clearingBrowserData) return { ok: false };
    const message = appearance().resolvedLanguage === 'zh-CN'
      ? '清除 ACEswarm 浏览数据？将退出网页账号并清除收藏、最近应用与网页缓存。'
      : 'Clear ACEswarm browser data? This signs you out of web pages and clears favorites, recent apps and cached pages.';
    if (!window.confirm(message)) return { ok: false };
    clearingBrowserData = true;
    localStorage.clear();
    sessionStorage.clear();
    return new Promise(resolve => {
      window.aceAndroidDataCleared = () => {
        delete window.aceAndroidDataCleared;
        resolve({ ok: true });
      };
      host.clearBrowserData();
    });
  },
  saveShellState: async state => write('desktop', state),
  desktopCommand: async action => {
    if (action === 'fullscreen') host.fullscreen();
    else if (action === 'quit') host.quit();
  },
  authorizeUrl: async url => ({ ok: validPage(url), error: 'Use an HTTP or HTTPS address.' }),
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
