// Roundtable Companion: a transparent, frameless, always-on-top window that shows only the agent's full-body
// figure on the desktop. It loads the portal in desk mode (https://roundtable.hrmsmrflrii.xyz/?desk=1), which
// runs the voice call itself, so no browser window is needed. Clicks pass through everywhere except the
// character's visible pixels and its buttons.
const { app, BrowserWindow, ipcMain, screen, session, Tray, Menu, nativeImage, globalShortcut, shell, Notification } = require('electron');
const fs = require('fs');
const path = require('path');

const PUBLIC = 'https://roundtable.hrmsmrflrii.xyz/';
// %APPDATA%\roundtable-companion\config.json = {"url": "http://192.168.40.16:8088/", "token": "<COMPANION_TOKEN>"}
// Token mode talks to the portal directly on the LAN (Google refuses SSO inside desktop apps, so Authentik is skipped);
// without a config it falls back to the Authentik-protected address and a sign-in window.
const CFG = (() => { try { return JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'config.json'), 'utf8')); } catch { return {}; } })();
const BASE = CFG.token && CFG.url ? CFG.url.replace(/\/?$/, '/') : PUBLIC;
const ORIGIN = new URL(BASE).origin;
const DESK_URL = BASE + '?desk=1';
const W = 780;

app.setAppUserModelId('Roundtable Companion');  // Windows toasts need an AppUserModelID
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
app.commandLine.appendSwitch('disable-renderer-backgrounding');
// the mic needs a secure context; the LAN portal is plain http
if (BASE.startsWith('http:')) app.commandLine.appendSwitch('unsafely-treat-insecure-origin-as-secure', ORIGIN);
// plain Chrome user agent so Authentik / Google sign-in pages treat it like a normal browser
app.userAgentFallback = app.userAgentFallback.replace(/ (Electron|roundtable-companion)\/\S+/g, '');

if (!app.requestSingleInstanceLock()) app.quit();

let win = null, login = null, tray = null, dragT = null, ready = false, STATE = null, teamPrev = null;

const loadState = () => { try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return {}; } };
const saveState = () => { if (win) fs.writeFileSync(STATE, JSON.stringify({ ...loadState(), pos: teamPrev ? [teamPrev.x, teamPrev.y] : win.getPosition(), hidden: !win.isVisible() })); };
const setPref = (k, v) => { fs.writeFileSync(STATE, JSON.stringify({ ...loadState(), [k]: v })); buildTray(); };

function geometry() {
  const wa = screen.getPrimaryDisplay().workArea;
  const h = Math.min(860, wa.height);
  let [x, y] = loadState().pos || [wa.x + wa.width - W, wa.y + wa.height - h];
  const onScreen = screen.getAllDisplays().some(d => x + W / 2 > d.workArea.x && x + W / 2 < d.workArea.x + d.workArea.width &&
    y + 100 > d.workArea.y && y + 100 < d.workArea.y + d.workArea.height);
  if (!onScreen) [x, y] = [wa.x + wa.width - W, wa.y + wa.height - h];
  return { x, y, width: W, height: h };
}

function createWindow() {
  win = new BrowserWindow({
    ...geometry(), show: false, transparent: true, frame: false, resizable: false, maximizable: false,
    hasShadow: false, skipTaskbar: true, alwaysOnTop: true, backgroundColor: '#00000000', title: 'Roundtable Companion',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), backgroundThrottling: false, contextIsolation: true },
  });
  win.setAlwaysOnTop(true, 'floating');
  win.setIgnoreMouseEvents(true, { forward: true });
  win.webContents.on('did-finish-load', checkPage);
  win.webContents.on('did-fail-load', (_e, code, _desc, _url, main) => {
    if (main && code !== -3) setTimeout(() => win && win.loadURL(DESK_URL), 15000);
  });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.on('moved', saveState);
  win.loadURL(DESK_URL);
}

// The portal sits behind Authentik: if the desk page did not load (sign-in page instead), sign in in a normal window.
async function checkPage() {
  // a reload drops the summoned team: give the full-screen window back its normal size
  if (teamPrev) { const b = teamPrev; teamPrev = null; win.setResizable(true); win.setBounds(b); win.setResizable(false); }
  let ok = false;
  try { ok = await win.webContents.executeJavaScript("!!document.getElementById('buddy') && document.documentElement.classList.contains('desk')"); } catch { }
  if (ok) {
    ready = true;
    if (!loadState().hidden) win.showInactive();
    if (login) { login.close(); login = null; }
  } else {
    ready = false;
    win.hide();
    if (CFG.token) { if (tray) tray.setToolTip('Roundtable Companion: portal unreachable, retrying…'); setTimeout(() => win && win.loadURL(DESK_URL), 30000); }
    else openLogin();
  }
  buildTray();
}

function openLogin() {
  if (CFG.token) { win && win.loadURL(DESK_URL); return; }
  if (login) { login.focus(); return; }
  login = new BrowserWindow({ width: 520, height: 760, title: 'Roundtable Companion: sign in', autoHideMenuBar: true });
  login.on('closed', () => { login = null; });
  login.webContents.on('did-finish-load', async () => {
    let ok = false;
    try { ok = await login.webContents.executeJavaScript("!!document.getElementById('buddy')"); } catch { }
    if (ok) { login.close(); win.loadURL(DESK_URL); }
  });
  login.loadURL(BASE);
}

function toggle() {
  if (!win) return;
  if (win.isVisible()) win.hide(); else if (ready) win.showInactive(); else openLogin();
  saveState();
  buildTray();
}

function cmd(c) { if (win && ready) { if (!win.isVisible()) { win.showInactive(); saveState(); buildTray(); } win.webContents.send('cmd', c); } }

const snoozed = () => (loadState().snooze || 0) > Date.now();

// Desk pop-up (portal desk mode asks before showing one): off -> never; snoozed -> urgent only. Shows the figure
// without stealing focus and raises a Windows toast; clicking the toast starts a call with Gideon about it.
function popup(ev) {
  const st = loadState();
  if (!win || !ready || st.popups === false || (snoozed() && ev.kind !== 'urgent')) return false;
  if (!win.isVisible()) { win.showInactive(); saveState(); buildTray(); }
  win.moveTop();
  if (Notification.isSupported()) {
    const n = new Notification({ title: ev.kind === 'urgent' ? '🚨 Gideon: urgent' : ev.kind === 'approval' ? '✅ Gideon: approval needed' : '📞 Gideon',
      body: String(ev.title || '').slice(0, 200), icon: path.join(__dirname, 'icon.png'), urgency: ev.kind === 'urgent' ? 'critical' : 'normal' });
    n.on('click', () => cmd('notetalk'));
    n.show();
  }
  return true;
}

function buildTray() {
  if (!tray) {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'icon.png')).resize({ width: 16, height: 16 });
    tray = new Tray(icon); tray.setToolTip('Roundtable Companion'); tray.on('click', toggle);
  }
  const auto = app.getLoginItemSettings({ path: process.execPath, args: [app.getAppPath()] });
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: win && win.isVisible() ? 'Hide companion' : 'Show companion', click: toggle },
    { label: 'Talk to Gideon / stop and hide  (Ctrl+Alt+A)', click: () => cmd('home') },
    { label: 'Talk / hang up  (Ctrl+Alt+T)', click: () => cmd('talk') },
    { label: 'Stop talking  (Ctrl+Alt+X)', click: () => cmd('cut') },
    { label: 'Mute mic  (Ctrl+Alt+M)', click: () => cmd('mute') },
    { label: 'Switch agent…', click: () => cmd('agents') },
    { label: 'Summon / dismiss the team  (Ctrl+Alt+G)', click: () => cmd('team') },
    { type: 'separator' },
    { label: 'Pop up when needed', type: 'checkbox', checked: loadState().popups !== false, click: (m) => setPref('popups', m.checked) },
    { label: snoozed() ? `Pop-ups snoozed until ${new Date(loadState().snooze).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} (urgent still shows)`
      : 'Snooze pop-ups for 1 hour', click: () => setPref('snooze', snoozed() ? 0 : Date.now() + 3600e3) },
    { type: 'separator' },
    { label: 'Open Roundtable portal', click: () => shell.openExternal(PUBLIC) },
    ...(CFG.token ? [] : [{ label: 'Sign in again', click: openLogin }]),
    { label: 'Reload', click: () => win && win.loadURL(DESK_URL) },
    { label: 'Reset position', click: () => {
      if (!win || teamPrev) return; const wa = screen.getPrimaryDisplay().workArea, [, h] = win.getSize();
      win.setPosition(wa.x + wa.width - W, wa.y + wa.height - h); saveState(); } },
    { label: 'Start with Windows', type: 'checkbox', checked: auto.openAtLogin,
      click: (m) => app.setLoginItemSettings({ openAtLogin: m.checked, path: process.execPath, args: [app.getAppPath()] }) },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]));
}

// Authentik's session cookies have no expiry, so they would vanish on every restart: keep them for 30 days.
function persistCookies() {
  const c = session.defaultSession.cookies;
  c.on('changed', (_e, k, _cause, removed) => {
    const dom = (k.domain || '').replace(/^\./, '');
    if (removed || k.expirationDate || !/(^|\.)hrmsmrflrii\.xyz$/.test(dom)) return;
    c.set({ url: `https://${dom}${k.path || '/'}`, name: k.name, value: k.value, domain: k.hostOnly ? undefined : k.domain,
      path: k.path, secure: k.secure, httpOnly: k.httpOnly, sameSite: k.sameSite,
      expirationDate: Math.floor(Date.now() / 1000) + 30 * 86400 }).catch(() => { });
  });
}

app.on('second-instance', () => { if (win && ready) { win.showInactive(); saveState(); buildTray(); } else openLogin(); });

app.whenReady().then(() => {
  STATE = path.join(app.getPath('userData'), 'window.json');
  const allow = ['media', 'mediaKeySystem', 'clipboard-sanitized-write', 'notifications'];
  session.defaultSession.setPermissionRequestHandler((_wc, p, cb) => cb(allow.includes(p)));
  session.defaultSession.setPermissionCheckHandler((_wc, p) => allow.includes(p));
  persistCookies();
  if (CFG.token) {
    session.defaultSession.webRequest.onBeforeSendHeaders({ urls: [ORIGIN + '/*'] }, (d, cb) => {
      d.requestHeaders['X-Companion-Token'] = CFG.token; cb({ requestHeaders: d.requestHeaders });
    });
  }

  ipcMain.on('clickthrough', (_e, on) => { if (win) on ? win.setIgnoreMouseEvents(true, { forward: true }) : win.setIgnoreMouseEvents(false); });
  // mirroring a browser call (browser minimized): show even if hidden from the tray, then restore
  let mirrorHid = false;
  ipcMain.on('mirror', (_e, on) => {
    if (!win || !ready) return;
    if (on) { mirrorHid = !win.isVisible(); if (mirrorHid) win.showInactive(); }
    else if (mirrorHid) { mirrorHid = false; win.hide(); }
  });
  ipcMain.handle('popup', (_e, ev) => popup(ev || {}));
  // summon_team: cover the whole work area of the companion's display so every agent can stand on the desktop;
  // the page keeps the window click-through except over the figures. Restored (not saved) when the team leaves.
  ipcMain.on('team', (_e, on) => {
    if (!win) return;
    if (on && !teamPrev) {
      teamPrev = win.getBounds();
      if (!win.isVisible()) win.showInactive();
      win.setResizable(true); win.setBounds(screen.getDisplayMatching(teamPrev).workArea); win.setResizable(false);
    } else if (!on && teamPrev) {
      const b = teamPrev; teamPrev = null;
      win.setResizable(true); win.setBounds(b); win.setResizable(false); saveState();
    }
  });
  ipcMain.on('hide', () => { if (win) { win.hide(); saveState(); buildTray(); } });
  ipcMain.on('drag', (_e, on) => {
    clearInterval(dragT); dragT = null;
    if (!on || !win || teamPrev) { saveState(); return; }
    const c = screen.getCursorScreenPoint(), [wx, wy] = win.getPosition(), ox = c.x - wx, oy = c.y - wy;
    dragT = setInterval(() => { const p = screen.getCursorScreenPoint(); win.setPosition(p.x - ox, p.y - oy); }, 12);
  });

  createWindow();
  buildTray();
  globalShortcut.register('Control+Alt+A', () => (ready ? cmd('home') : openLogin()));  // show Gideon and start listening; pressed again during a call it hangs up and hides him
  globalShortcut.register('Control+Alt+T', () => cmd('talk'));
  globalShortcut.register('Control+Alt+X', () => cmd('cut'));
  globalShortcut.register('Control+Alt+M', () => cmd('mute'));
  globalShortcut.register('Control+Alt+G', () => cmd('team'));
});

app.on('will-quit', () => { globalShortcut.unregisterAll(); session.defaultSession.cookies.flushStore().catch(() => { }); });
// closing the sign-in window must not quit the app (it lives in the tray)
app.on('window-all-closed', () => { });
