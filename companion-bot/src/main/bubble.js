import { EventEmitter } from 'events'
import { ipcMain, screen } from 'electron'
import { config } from '../shared/config'
import { setBotState } from './botState'
import { createOverlayWindow, loadPage } from './overlayWindow'

// The speech bubble: a second small transparent window that sits above the
// bot (or below it when there's no room) and follows it around.
//
// Its content lives here in the main process, not in the bubble's page, so
// anything (a click, later the demo or an external agent) can show a message
// with showBubble() and the page just renders whatever it is sent.

const cfg = config.bubble
const events = new EventEmitter()
const emptyContent = { message: '', input: false, choices: null, questionId: null }
let bot = null
let win = null
let height = 120 // replaced by the page's measured height once it renders
let content = emptyContent
let placement = { side: 'above', tailX: cfg.width / 2 }
let replyIndex = 0

const clamp = (v, min, max) => Math.min(max, Math.max(min, v))
const fromBubble = (event) => win && event.sender === win.webContents

function ensureWindow() {
  if (win && !win.isDestroyed()) return win
  win = createOverlayWindow({ width: cfg.width, height })
  win.on('closed', () => {
    win = null
  })
  loadPage(win, 'bubble.html')
  return win
}

export function isBubbleVisible() {
  return Boolean(win && !win.isDestroyed() && win.isVisible())
}

// content: { message, input?: show the text box, choices?: [labels],
//           questionId?: which question the choice buttons answer (question.js) }
// focus: give the bubble keyboard focus (so the user can type straight away).
export function showBubble(newContent, { focus = true } = {}) {
  content = { ...emptyContent, ...newContent }
  const w = ensureWindow()
  w.webContents.send('bubble:content', content)
  reposition()
  if (!w.isVisible()) {
    if (focus) w.show()
    else w.showInactive()
  } else if (focus) {
    w.focus()
  }
}

export function hideBubble() {
  if (win && !win.isDestroyed()) win.hide()
}

// The user closed the bubble (x, Esc, clicking the bot, hiding the bot).
// Unlike hideBubble(), this tells listeners, so e.g. a pending question can
// be marked as dismissed.
function closeBubbleByUser() {
  if (!isBubbleVisible()) return
  hideBubble()
  events.emit('closed-by-user', content)
}

export function onBubbleClosedByUser(listener) {
  events.on('closed-by-user', listener)
}

export function onBubbleChoice(listener) {
  events.on('choice', listener)
}

export function toggleBubble() {
  if (isBubbleVisible()) closeBubbleByUser()
  else showBubble({ message: cfg.greeting, input: true })
}

// Place the bubble centred over the bot, kept inside the work area. If it
// doesn't fit above, put it below. The tail is moved to keep pointing at the
// bot even when the bubble is pushed sideways by a screen edge.
function reposition() {
  if (!win || win.isDestroyed() || !bot || bot.isDestroyed()) return
  const b = bot.getBounds()
  const { workArea: wa } = screen.getPrimaryDisplay()
  const w = cfg.width
  const botCentreX = b.x + b.width / 2

  const x = clamp(Math.round(botCentreX - w / 2), wa.x, wa.x + wa.width - w)
  let side = 'above'
  let y = b.y - height - cfg.gap
  if (y < wa.y) {
    side = 'below'
    y = b.y + b.height + cfg.gap
  }
  y = clamp(y, wa.y, wa.y + wa.height - height)

  // Fixed size passed every time: avoids DPI rounding drift (see wander.js).
  win.setBounds({ x, y, width: w, height })

  const tailX = Math.round(clamp(botCentreX - x, cfg.tailInset, w - cfg.tailInset))
  if (side !== placement.side || tailX !== placement.tailX) {
    placement = { side, tailX }
    win.webContents.send('bubble:placement', placement)
  }
}

function nextCannedReply() {
  const reply = cfg.cannedReplies[replyIndex % cfg.cannedReplies.length]
  replyIndex++
  return reply
}

// --- IPC from the bubble page ---------------------------------------------

ipcMain.handle('bubble:get-initial', (event) => (fromBubble(event) ? { content, placement } : null))

// The page measured its content; size the window to fit exactly, so there's
// no invisible margin blocking clicks around the bubble.
ipcMain.on('bubble:resize', (event, newHeight) => {
  if (!fromBubble(event) || !Number.isFinite(newHeight)) return
  height = clamp(Math.ceil(newHeight), 40, cfg.maxHeight)
  reposition()
})

ipcMain.on('bubble:submit', (event, text) => {
  if (!fromBubble(event) || typeof text !== 'string' || !text.trim()) return
  if (text.length > cfg.maxInputLength) return
  // No real agent yet: answer with a canned reply while the bot "talks".
  setBotState('talking')
  showBubble({ message: nextCannedReply(), input: true }, { focus: false })
})

ipcMain.on('bubble:close', (event) => {
  if (fromBubble(event)) closeBubbleByUser()
})

// A choice button was clicked. Only accept it for the question currently on
// screen, so a late click can't answer a newer question.
ipcMain.on('bubble:choose', (event, questionId, index) => {
  if (!fromBubble(event) || questionId !== content.questionId || !content.choices) return
  if (!Number.isInteger(index) || index < 0 || index >= content.choices.length) return
  events.emit('choice', { questionId, index })
})

// --- Wiring --------------------------------------------------------------

export function setupBubble(botWindow) {
  bot = botWindow
  // Create (hidden) and load the page now, so the first message appears
  // instantly instead of waiting for the page to load.
  ensureWindow()
  // Follow the bot: fires for wandering, dragging and any other move.
  bot.on('move', reposition)
  bot.on('hide', closeBubbleByUser)
  screen.on('display-metrics-changed', reposition)
}
