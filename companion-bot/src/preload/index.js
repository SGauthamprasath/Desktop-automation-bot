import { contextBridge, ipcRenderer } from 'electron'

// Subscribe to a main->renderer channel; returns an unsubscribe function.
function subscribe(channel, callback) {
  const listener = (_event, ...args) => callback(...args)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

// The only bridge between our pages and Electron. Pages have no Node access;
// they can call exactly these functions and nothing else. Main additionally
// checks which window each bot:/bubble: message comes from.
contextBridge.exposeInMainWorld('botApi', {
  // true = let clicks fall through to the apps underneath; false = catch them.
  setClickThrough: (clickThrough) => ipcRenderer.send('bot:set-click-through', Boolean(clickThrough)),
  // Main tells us it restored click-through because the cursor left the
  // window without the page seeing a mouseleave.
  onPointerLeft: (callback) => subscribe('bot:pointer-left', callback),

  // Pointer interaction on the bot.
  dragStart: (offset) => ipcRenderer.send('bot:drag-start', offset),
  dragEnd: () => ipcRenderer.send('bot:drag-end'),
  click: () => ipcRenderer.send('bot:click'),
  showContextMenu: () => ipcRenderer.send('bot:context-menu'),

  // Bot state machine (owned by the main process).
  setBotState: (name, options) => ipcRenderer.invoke('bot:set-state', name, options),
  getBotState: () => ipcRenderer.invoke('bot:get-state'),
  onBotState: (callback) => subscribe('bot:state', callback)
})

contextBridge.exposeInMainWorld('bubbleApi', {
  getInitial: () => ipcRenderer.invoke('bubble:get-initial'),
  onContent: (callback) => subscribe('bubble:content', callback),
  onPlacement: (callback) => subscribe('bubble:placement', callback),
  resize: (height) => ipcRenderer.send('bubble:resize', Number(height)),
  submit: (text) => ipcRenderer.send('bubble:submit', String(text)),
  choose: (questionId, index) => ipcRenderer.send('bubble:choose', questionId, index),
  close: () => ipcRenderer.send('bubble:close')
})
