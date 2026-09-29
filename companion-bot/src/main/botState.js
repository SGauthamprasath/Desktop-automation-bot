import { EventEmitter } from 'events'
import sheet from '../../assets/bot_spritesheet.json'
import { config } from '../shared/config'

// The bot's state machine. Lives in the main process so there is exactly one
// source of truth; windows only receive copies (see index.js).
//
// A state is any row named in the sprite JSON. Options:
//   hold:       true = never auto-return to idle (for temporary states)
//   durationMs: override the auto-return timeout from config.stateTimeouts
//   facing:     'left' | 'right' (sprite is flipped when 'left')
// Unknown options are ignored, so callers (tray, IPC, later an HTTP agent)
// can pass extra data without breaking anything.

const events = new EventEmitter()
let returnTimer = null
let current = { name: 'idle', facing: 'right', hold: false, until: null }

export const stateNames = Object.keys(sheet.states)

export function getBotState() {
  return current
}

export function onBotStateChange(listener) {
  events.on('change', listener)
  return () => events.off('change', listener)
}

export function setBotState(name, options = {}) {
  if (!stateNames.includes(name)) throw new Error(`Unknown bot state "${name}"`)
  if (options === null || typeof options !== 'object') options = {}

  clearTimeout(returnTimer)
  returnTimer = null

  const facing = options.facing === 'left' || options.facing === 'right' ? options.facing : current.facing
  const hold = options.hold === true
  // Temporary states (those with a timeout in config) return to idle unless held.
  const duration = Number.isFinite(options.durationMs) ? options.durationMs : config.stateTimeouts[name]
  const temporary = !hold && duration > 0

  current = { name, facing, hold, until: temporary ? Date.now() + duration : null }
  if (temporary) returnTimer = setTimeout(() => setBotState('idle'), duration)

  events.emit('change', current)
  return current
}

// Change only the facing direction, keeping the current state and its timer.
export function setFacing(facing) {
  if (facing !== 'left' && facing !== 'right') return current
  if (facing === current.facing) return current
  current = { ...current, facing }
  events.emit('change', current)
  return current
}
