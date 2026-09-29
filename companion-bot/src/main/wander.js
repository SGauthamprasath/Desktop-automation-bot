import { EventEmitter } from 'events'
import { screen } from 'electron'
import sheet from '../../assets/bot_spritesheet.json'
import { config } from '../shared/config'
import { getBotState, onBotStateChange, setBotState } from './botState'

// Idle roaming: rest, walk to a random nearby point, rest, repeat.
//
// Runs only while the bot is idle and wandering is enabled. Any other state
// (set by the tray, a shortcut, later the agent) stops a walk mid-way and
// cancels the next one; returning to idle schedules it again.
//
// Coordinates: everything is in DIP (device-independent pixels). The work
// area, BrowserWindow bounds and our targets all use DIP, so Windows display
// scaling (125%, 150%...) never enters the maths; Electron converts to
// physical pixels itself.

const cfg = config.wander
const events = new EventEmitter()

let win = null
let enabled = cfg.enabledAtStart
let restTimer = null
let walk = null // { timer } while a walk is in progress
let selfChange = false // true while *we* are calling setBotState
const blockers = new Set() // reasons wandering is temporarily blocked, e.g. 'drag'

const random = (min, max) => min + Math.random() * (max - min)
// The window's size never changes; always use these instead of reading it
// back with getBounds(). At fractional scale factors (e.g. 125%) Windows can
// round an odd-positioned window 1px larger, and feeding that size back into
// setBounds() makes the bot slowly grow with every walk.
const width = sheet.frameWidth
const height = sheet.frameHeight

const clamp = (v, min, max) => Math.min(max, Math.max(min, v))
// Ease in and out so the bot accelerates and stops gently.
const easeInOut = (t) => 0.5 - Math.cos(Math.PI * t) / 2

export function isWandering() {
  return enabled
}

export function onWanderChange(listener) {
  events.on('change', listener)
}

export function setWandering(on) {
  enabled = Boolean(on)
  if (enabled) scheduleNext()
  else stop({ settle: true })
  events.emit('change', enabled)
}

// Temporarily stop wandering for some reason (e.g. the user is dragging the
// bot) without touching the user's Pause/Resume choice.
export function blockWandering(reason) {
  blockers.add(reason)
  stop({ settle: true })
}

export function unblockWandering(reason) {
  blockers.delete(reason)
  scheduleNext()
}

// Cancel the pending rest and any walk in progress. `settle` puts the bot back
// to idle if we were mid-walk (used when *we* stop it: pause, hide, drag).
// Without it the state is left alone, because someone else just set it.
function stop({ settle = false } = {}) {
  clearTimeout(restTimer)
  restTimer = null
  if (walk) {
    clearInterval(walk.timer)
    walk = null
    if (settle && getBotState().name === 'walk') {
      selfChange = true
      setBotState('idle')
      selfChange = false
    }
  }
}

function canWander() {
  return (
    enabled &&
    blockers.size === 0 &&
    win &&
    !win.isDestroyed() &&
    win.isVisible() &&
    getBotState().name === 'idle'
  )
}

// After a random rest, start a walk (if still allowed at that moment).
function scheduleNext() {
  stop()
  if (!canWander()) return
  restTimer = setTimeout(startWalk, random(cfg.pauseMinMs, cfg.pauseMaxMs))
}

// Top-left positions the window may occupy: fully inside the primary work
// area (which already excludes the taskbar).
function allowedArea() {
  const { workArea } = screen.getPrimaryDisplay()
  return {
    minX: workArea.x,
    minY: workArea.y,
    maxX: workArea.x + workArea.width - width,
    maxY: workArea.y + workArea.height - height
  }
}

function pickTarget(from) {
  const a = allowedArea()
  const box = {
    minX: Math.max(a.minX, from.x - cfg.maxHop),
    maxX: Math.min(a.maxX, from.x + cfg.maxHop),
    minY: Math.max(a.minY, from.y - cfg.maxHop),
    maxY: Math.min(a.maxY, from.y + cfg.maxHop)
  }
  let target
  for (let i = 0; i < 10; i++) {
    target = { x: Math.round(random(box.minX, box.maxX)), y: Math.round(random(box.minY, box.maxY)) }
    if (Math.hypot(target.x - from.x, target.y - from.y) >= cfg.minHop) break
  }
  return target
}

function startWalk() {
  restTimer = null
  if (!canWander()) return

  const { x, y } = win.getBounds()
  const from = { x, y }
  const to = pickTarget(from)
  const distance = Math.hypot(to.x - from.x, to.y - from.y)
  const duration = clamp((distance / cfg.speed) * 1000, cfg.moveMinMs, cfg.moveMaxMs)
  const facing = to.x < from.x ? 'left' : 'right'

  selfChange = true
  setBotState('walk', { facing })
  selfChange = false

  const start = Date.now()
  walk = {
    timer: setInterval(() => {
      if (!win || win.isDestroyed()) return stop()
      const t = Math.min(1, (Date.now() - start) / duration)
      const k = easeInOut(t)
      // Pass the fixed size with every move so any rounding can't stick.
      win.setBounds({
        x: Math.round(from.x + (to.x - from.x) * k),
        y: Math.round(from.y + (to.y - from.y) * k),
        width,
        height
      })
      if (t === 1) {
        clearInterval(walk.timer)
        walk = null
        selfChange = true
        setBotState('idle')
        selfChange = false
        scheduleNext()
      }
    }, cfg.tickMs)
  }
}

// Keep the bot inside the work area if the screen changes under it
// (resolution, scaling, taskbar moved or resized).
function clampIntoWorkArea() {
  if (!win || win.isDestroyed()) return
  const a = allowedArea()
  const b = win.getBounds()
  const x = clamp(b.x, a.minX, a.maxX)
  const y = clamp(b.y, a.minY, a.maxY)
  if (x !== b.x || y !== b.y) win.setBounds({ x, y, width, height })
}

export function startWandering(botWindow) {
  win = botWindow
  onBotStateChange((state) => {
    if (selfChange) return
    // Someone else changed the state: stop whatever we were doing, and
    // start the rest/walk cycle again only if we're back to idle.
    if (state.name === 'idle') scheduleNext()
    else stop()
  })
  win.on('show', scheduleNext)
  win.on('hide', () => stop({ settle: true }))
  screen.on('display-metrics-changed', clampIntoWorkArea)
  screen.on('display-removed', clampIntoWorkArea)
  scheduleNext()
}
