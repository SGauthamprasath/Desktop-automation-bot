import { app, BrowserWindow, ipcMain } from 'electron'
import { getBotState, onBotStateChange, setBotState } from './botState'
import { createBotWindow, onBotClick, onBotContextMenu, onDragChange } from './botWindow'
import { setupBubble, toggleBubble } from './bubble'
import { setupDevControls } from './devControls'
import { startHttpServer } from './httpServer'
import { popupBotMenu } from './menus'
import { blockWandering, startWandering, unblockWandering } from './wander'

// Entry point: wires the modules together. Each module owns one concern:
//   botState    - the state machine (single source of truth)
//   botWindow   - bot window, click-through, dragging
//   bubble      - speech bubble window
//   wander      - idle roaming
//   question    - ask the user with choice buttons
//   demo        - scripted ask-vs-act sequence
//   httpServer  - local API for an external agent
//   devControls - tray + global shortcuts;  menus - shared menu items

const debug = Boolean(process.env.BOT_DEBUG)

// --- Bot state over IPC --------------------------------------------------
// Any window can read or change the state; every change is pushed to all
// windows, so they never keep their own copy of the truth.
ipcMain.handle('bot:get-state', () => getBotState())
ipcMain.handle('bot:set-state', (_event, name, options) => {
  try {
    return setBotState(name, options)
  } catch (err) {
    console.warn('[bot]', err.message)
    return getBotState()
  }
})
onBotStateChange((state) => {
  if (debug) console.log('[bot] state', state)
  for (const win of BrowserWindow.getAllWindows()) win.webContents.send('bot:state', state)
})

app.whenReady().then(() => {
  const bot = createBotWindow()
  setupBubble(bot)
  setupDevControls()
  startWandering(bot)
  startHttpServer()

  // While the user drags the bot, don't let it walk off on its own.
  onDragChange((dragging) => (dragging ? blockWandering('drag') : unblockWandering('drag')))
  onBotClick(toggleBubble)
  onBotContextMenu(popupBotMenu)
})

app.on('window-all-closed', () => app.quit())
