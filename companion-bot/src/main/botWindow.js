import { EventEmitter } from 'events'
import { ipcMain, screen } from 'electron'
import sheet from '../../assets/bot_spritesheet.json'
import { config } from '../shared/config'
import { createOverlayWindow, loadPage } from './overlayWindow'

// The bot's window: click-through handling, manual dragging, visibility.

const debug = Boolean(process.env.BOT_DEBUG)
const events = new EventEmitter()
const width = sheet.frameWidth
const height = sheet.frameHeight

let win = null
let drag = null // { offsetX, offsetY, timer } while the user is dragging

export function getBotWindow() {
  return win
}

export function createBotWindow() {
  // workArea excludes the taskbar. All values are in DIP (device-independent
  // pixels), the same unit BrowserWindow uses, so DPI scaling is handled for us.
  const { workArea } = screen.getPrimaryDisplay()
  win = createOverlayWindow({
    width,
    height,
    x: workArea.x + workArea.width - width - config.startMargin,
    y: workArea.y + workArea.height - height - config.startMargin
  })

  // Start fully click-through. `forward: true` still delivers mousemove events
  // to the page, so the renderer can tell when the cursor is over the sprite
  // and ask us to start catching clicks (see 'bot:set-click-through').
  win.setIgnoreMouseEvents(true, { forward: true })

  // showInactive: appear without stealing focus from the app the user is in.
  win.once('ready-to-show', () => win.showInactive())
  win.on('show', () => events.emit('visibility', true))
  win.on('hide', () => events.emit('visibility', false))
  win.on('closed', () => {
    win = null
  })
  loadPage(win, 'index.html')

  if (debug) {
    win.webContents.on('did-finish-load', () => {
      console.log('[bot] window bounds', win.getBounds(), 'workArea', workArea)
    })
  }
  return win
}

// --- Visibility ----------------------------------------------------------

export function isBotVisible() {
  return Boolean(win && win.isVisible())
}

export function setBotVisible(visible) {
  if (!win) return
  if (visible) win.showInactive()
  else win.hide()
}

export function onBotVisibilityChange(listener) {
  events.on('visibility', listener)
}

// Only accept bot-window messages from the bot window's own page.
const fromBot = (event) => win && event.sender === win.webContents

// --- Click-through -------------------------------------------------------

// The renderer hit-tests the sprite on every forwarded mousemove and tells us
// whether the cursor is over a transparent pixel (click-through) or not.
ipcMain.on('bot:set-click-through', (event, clickThrough) => {
  if (!fromBot(event) || typeof clickThrough !== 'boolean') return
  if (clickThrough) {
    win.setIgnoreMouseEvents(true, { forward: true })
    stopLeaveWatch()
  } else {
    win.setIgnoreMouseEvents(false)
    startLeaveWatch()
  }
  if (debug) console.log('[bot] click-through', clickThrough)
})

// Safety net: while the window is catching the mouse, Windows does not always
// deliver `mouseleave` if the cursor jumps out of it in a single move. The page
// would then never ask for click-through again, leaving an invisible square
// that blocks clicks. So while catching, poll the real cursor position and
// restore click-through ourselves once it is outside the window.
let leaveWatch = null

function startLeaveWatch() {
  stopLeaveWatch()
  leaveWatch = setInterval(() => {
    if (!win || win.isDestroyed()) return stopLeaveWatch()
    // During a drag the cursor can briefly be outside (e.g. window pinned at
    // a screen edge); the page holds pointer capture, so leave it alone.
    if (drag) return
    const { x, y } = screen.getCursorScreenPoint() // DIP, same as getBounds()
    const b = win.getBounds()
    if (x < b.x || y < b.y || x >= b.x + b.width || y >= b.y + b.height) {
      stopLeaveWatch()
      win.setIgnoreMouseEvents(true, { forward: true })
      win.webContents.send('bot:pointer-left')
      if (debug) console.log('[bot] click-through true (cursor left, restored by main)')
    }
  }, config.leaveCheckMs)
}

function stopLeaveWatch() {
  clearInterval(leaveWatch)
  leaveWatch = null
}

// --- Dragging ------------------------------------------------------------
// We don't use -webkit-app-region: drag, because Windows then treats the area
// as a title bar and the page never receives click events. Instead the page
// decides "this is a drag" (movement past a small threshold) and tells us
// where inside the window it was grabbed; we then move the window so that
// point stays under the real cursor, polling the cursor from here rather
// than relaying every mousemove over IPC.

export function isDragging() {
  return Boolean(drag)
}

export function onDragChange(listener) {
  events.on('drag', listener)
}

ipcMain.on('bot:drag-start', (event, offset) => {
  if (!fromBot(event) || drag) return
  const offsetX = Number(offset?.offsetX)
  const offsetY = Number(offset?.offsetY)
  if (!Number.isFinite(offsetX) || !Number.isFinite(offsetY)) return

  drag = { offsetX, offsetY, timer: setInterval(dragTick, config.dragTickMs) }
  events.emit('drag', true)
  if (debug) console.log('[bot] drag start', { offsetX, offsetY })
})

ipcMain.on('bot:drag-end', (event) => {
  if (!fromBot(event)) return
  endDrag()
})

function dragTick() {
  if (!win || win.isDestroyed()) return endDrag()
  const cursor = screen.getCursorScreenPoint()
  const { workArea: wa } = screen.getPrimaryDisplay()
  // Keep the whole bot inside the work area (never behind the taskbar).
  const x = Math.min(Math.max(Math.round(cursor.x - drag.offsetX), wa.x), wa.x + wa.width - width)
  const y = Math.min(Math.max(Math.round(cursor.y - drag.offsetY), wa.y), wa.y + wa.height - height)
  // Always pass the fixed size (see wander.js: avoids DPI rounding drift).
  win.setBounds({ x, y, width, height })
}

function endDrag() {
  if (!drag) return
  clearInterval(drag.timer)
  drag = null
  events.emit('drag', false)
  if (debug) console.log('[bot] drag end', win?.getBounds())
}

// --- Click and right-click ----------------------------------------------

export function onBotClick(listener) {
  events.on('click', listener)
}

export function onBotContextMenu(listener) {
  events.on('context-menu', listener)
}

ipcMain.on('bot:click', (event) => {
  if (fromBot(event)) events.emit('click')
})

ipcMain.on('bot:context-menu', (event) => {
  if (fromBot(event)) events.emit('context-menu', win)
})
