import { BrowserWindow } from 'electron'
import { join } from 'path'

// Shared setup for our transparent, frameless, always-on-top windows (the bot
// and the speech bubble).
export function createOverlayWindow(options) {
  return new BrowserWindow({
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    show: false,
    ...options,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // Keep animations running even when Chromium thinks the window is in
      // the background.
      backgroundThrottling: false
    }
  })
}

// Load one of the renderer's HTML pages: from the Vite dev server during
// `npm run dev`, from the built files otherwise.
export function loadPage(win, page) {
  if (process.env.ELECTRON_RENDERER_URL) {
    return win.loadURL(`${process.env.ELECTRON_RENDERER_URL}/${page}`)
  }
  return win.loadFile(join(__dirname, '../renderer', page))
}
