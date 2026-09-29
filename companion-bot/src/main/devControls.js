import { app, globalShortcut, Menu, nativeImage, Tray } from 'electron'
import { join } from 'path'
import sheet from '../../assets/bot_spritesheet.json'
import { config } from '../shared/config'
import { getBotState, onBotStateChange, setBotState, setFacing, stateNames } from './botState'
import { onBotVisibilityChange } from './botWindow'
import { isDemoRunning, onDemoChange, playDemo } from './demo'
import { controlItems } from './menus'
import { onWanderChange } from './wander'

// Developer controls: a tray icon whose menu triggers each state, and global
// shortcuts. Both go through setBotState like everything else.

let tray = null // module-level so it is not garbage-collected (icon would vanish)
let holdTemporary = false // tray/shortcut option: keep alert/talking until changed

function trigger(name) {
  setBotState(name, { hold: holdTemporary })
}

// Tray icon = first idle frame of the sprite sheet, cropped and scaled down.
function createTrayIcon() {
  const image = nativeImage.createFromPath(join(app.getAppPath(), 'assets', sheet.image))
  const { row } = sheet.states.idle
  return image
    .crop({ x: 0, y: row * sheet.frameHeight, width: sheet.frameWidth, height: sheet.frameHeight })
    .resize({ width: 16, height: 16, quality: 'best' })
}

function buildMenu() {
  const state = getBotState()
  const timed = (name) => (config.stateTimeouts[name] ? ` (${config.stateTimeouts[name] / 1000}s)` : '')
  return Menu.buildFromTemplate([
    { label: `State: ${state.name}${state.hold ? ' (held)' : ''}`, enabled: false },
    {
      label: isDemoRunning() ? 'Demo running…' : 'Play demo',
      enabled: !isDemoRunning(),
      click: () => playDemo()
    },
    { type: 'separator' },
    ...stateNames.map((name) => {
      const index = config.shortcutStates.indexOf(name)
      return {
        label: name + timed(name),
        type: 'radio',
        checked: state.name === name,
        accelerator: index >= 0 ? `${config.shortcutModifier}+${index + 1}` : undefined,
        registerAccelerator: false, // already registered globally below; this only shows the hint
        click: () => trigger(name)
      }
    }),
    { type: 'separator' },
    {
      label: 'Hold temporary states',
      type: 'checkbox',
      checked: holdTemporary,
      click: (item) => {
        holdTemporary = item.checked
      }
    },
    {
      label: 'Face left',
      type: 'checkbox',
      checked: state.facing === 'left',
      click: (item) => setFacing(item.checked ? 'left' : 'right')
    },
    { type: 'separator' },
    ...controlItems() // Pause/Resume Wandering, Hide/Show, Quit
  ])
}

function registerShortcuts() {
  config.shortcutStates.forEach((name, i) => {
    const accelerator = `${config.shortcutModifier}+${i + 1}`
    // register() returns false if another app already owns the combination.
    if (!globalShortcut.register(accelerator, () => trigger(name))) {
      console.warn(`[bot] could not register global shortcut ${accelerator}`)
    }
  })
}

export function setupDevControls() {
  tray = new Tray(createTrayIcon())
  tray.setToolTip('Companion Bot')
  tray.setContextMenu(buildMenu())
  // Rebuild so the radio check and "State:" line follow the current state.
  onBotStateChange(() => tray.setContextMenu(buildMenu()))
  onWanderChange(() => tray.setContextMenu(buildMenu()))
  onBotVisibilityChange(() => tray.setContextMenu(buildMenu()))
  onDemoChange(() => tray.setContextMenu(buildMenu()))

  registerShortcuts()
  app.on('will-quit', () => globalShortcut.unregisterAll())
}
