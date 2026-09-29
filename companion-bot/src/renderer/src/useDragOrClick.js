import { useRef, useState } from 'react'
import { config } from '../../shared/config'

// Tells a click from a drag on the bot.
//
// A left-button press becomes a drag once the pointer has moved more than
// config.dragThresholdPx; releasing before that is a click. Pointer capture
// keeps events coming to us even if the cursor outruns the window.
// The window itself is moved by the main process (see botWindow.js); we only
// report where inside the window the bot was grabbed.
export function useDragOrClick() {
  const press = useRef(null) // { x, y, offsetX, offsetY, dragging } while the button is down
  const [pressed, setPressed] = useState(false)
  const [dragging, setDragging] = useState(false)

  const finish = (asClick) => {
    const p = press.current
    if (!p) return
    press.current = null
    setPressed(false)
    if (p.dragging) {
      setDragging(false)
      window.botApi.dragEnd()
    } else if (asClick) {
      window.botApi.click()
    }
  }

  const handlers = {
    onPointerDown: (e) => {
      if (e.button !== 0) return
      e.currentTarget.setPointerCapture(e.pointerId)
      press.current = {
        x: e.screenX, // screen coords: unaffected by the window moving under us
        y: e.screenY,
        offsetX: e.clientX, // grab point inside the window, in DIP
        offsetY: e.clientY,
        dragging: false
      }
      setPressed(true)
    },
    onPointerMove: (e) => {
      const p = press.current
      if (!p || p.dragging) return
      if (Math.hypot(e.screenX - p.x, e.screenY - p.y) > config.dragThresholdPx) {
        p.dragging = true
        setDragging(true)
        window.botApi.dragStart({ offsetX: p.offsetX, offsetY: p.offsetY })
      }
    },
    onPointerUp: (e) => {
      if (e.button === 0) finish(true)
    },
    // Capture lost without a normal release (e.g. window hidden): end quietly.
    onLostPointerCapture: () => finish(false),
    onPointerCancel: () => finish(false),
    onContextMenu: (e) => {
      e.preventDefault()
      window.botApi.showContextMenu()
    }
  }

  return { pressed, dragging, handlers }
}
