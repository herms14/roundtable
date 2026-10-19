const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desk', {
  clickThrough: (on) => ipcRenderer.send('clickthrough', !!on),
  drag: (on) => ipcRenderer.send('drag', !!on),
  hide: () => ipcRenderer.send('hide'),
  popup: (ev) => ipcRenderer.invoke('popup', ev),
  team: (on) => ipcRenderer.send('team', !!on),
  mirror: (on) => ipcRenderer.send('mirror', !!on),
  onCmd: (fn) => ipcRenderer.on('cmd', (_e, c) => fn(c)),
});
