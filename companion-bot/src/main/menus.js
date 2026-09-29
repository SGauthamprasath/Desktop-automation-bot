import { app, Menu } from 'electron'
import { isBotVisible, setBotVisible } from './botWindow'
import { isWandering, setWandering } from './wander'

// Menu items shared by the bot's right-click menu and the tray menu.
// Built fresh each time so the labels reflect the current state.
export function controlItems() {
  return [
    {
      label: isWandering() ? 'Pause Wandering' : 'Resume Wandering',
      click: () => setWandering(!isWandering())
    },
    {
      label: isBotVisible() ? 'Hide' : 'Show',
      click: () => setBotVisible(!isBotVisible())
    },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() }
  ]
}

// Native context menu shown when the bot is right-clicked.
export function popupBotMenu(win) {
  Menu.buildFromTemplate(controlItems()).popup({ window: win })
}
