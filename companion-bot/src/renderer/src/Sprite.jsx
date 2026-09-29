import { useEffect, useRef, useState } from 'react'
import { sheet, fpsFor, frameOrigin, loadSheet, isOpaqueAt } from './spriteSheet'

// Draws one state's animation on a canvas at the sheet's native frame size
// (CSS scales it up with nearest-neighbour), and keeps the window's
// click-through setting in sync with whether the cursor is over the bot.
// `locked`: the mouse button is held on the bot (click or drag in progress);
// keep catching the mouse until it's released, whatever pixel is under it.
export default function Sprite({ state, flipped = false, locked = false }) {
  const canvasRef = useRef(null)
  const [loaded, setLoaded] = useState(null)
  const frameRef = useRef(0) // frame currently on screen
  const timelineRef = useRef({ state: null, start: 0 }) // when the current state's cycle began
  const pointerRef = useRef(null) // last cursor position in frame pixels, or null if outside
  const clickThroughRef = useRef(true) // mirrors the main process (starts click-through)

  useEffect(() => {
    loadSheet().then(setLoaded, (err) => console.error(err))
  }, [])

  // Re-evaluate click-through from the last known cursor position. Only sends
  // IPC when the answer actually changes.
  const updateClickThrough = () => {
    if (locked) return
    const p = pointerRef.current
    const overBot =
      !!loaded && !!p && isOpaqueAt(loaded, state, frameRef.current, p.x, p.y, flipped)
    document.body.classList.toggle('over-bot', overBot)
    if (clickThroughRef.current === overBot) {
      clickThroughRef.current = !overBot
      window.botApi.setClickThrough(!overBot)
    }
  }
  // Handlers below are registered once per effect run; route through a ref so
  // they always call the latest closure (current state/flipped/loaded).
  const updateRef = useRef(updateClickThrough)
  updateRef.current = updateClickThrough

  // Button released: re-check what's under the cursor now.
  useEffect(() => {
    if (!locked) updateRef.current()
  }, [locked])

  // Animation loop. Frame index is derived from elapsed time, so the fps is
  // exact regardless of the display's refresh rate.
  useEffect(() => {
    if (!loaded) return
    const ctx = canvasRef.current.getContext('2d')
    ctx.imageSmoothingEnabled = false
    const { frameWidth: fw, frameHeight: fh } = sheet
    const frameCount = sheet.states[state].frames // never index past this: extra cells are empty
    const fps = fpsFor(state)
    // Restart the cycle only when the state changes, not when just the
    // facing flips (turning mid-walk shouldn't reset the walk cycle).
    if (timelineRef.current.state !== state) {
      timelineRef.current = { state, start: performance.now() }
    }
    const { start } = timelineRef.current
    let shown = -1
    let raf

    const tick = (now) => {
      const index = Math.floor(((now - start) * fps) / 1000) % frameCount
      if (index !== shown) {
        shown = index
        frameRef.current = index
        const { sx, sy } = frameOrigin(state, index)
        ctx.clearRect(0, 0, fw, fh)
        ctx.save()
        if (flipped) {
          ctx.translate(fw, 0)
          ctx.scale(-1, 1)
        }
        ctx.drawImage(loaded.image, sx, sy, fw, fh, 0, 0, fw, fh)
        ctx.restore()
        // The cursor may not have moved, but the pixel under it may have
        // changed from opaque to transparent (or back).
        updateRef.current()
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [loaded, state, flipped])

  // Cursor tracking. While the window is click-through these events still
  // arrive because main uses setIgnoreMouseEvents(true, { forward: true }).
  useEffect(() => {
    const canvas = canvasRef.current
    const onMove = (e) => {
      const rect = canvas.getBoundingClientRect()
      pointerRef.current = {
        x: Math.floor(((e.clientX - rect.left) * sheet.frameWidth) / rect.width),
        y: Math.floor(((e.clientY - rect.top) * sheet.frameHeight) / rect.height)
      }
      updateRef.current()
    }
    const onLeave = () => {
      pointerRef.current = null
      updateRef.current()
    }
    // Windows fires a spurious mouseleave right after the window switches from
    // click-through to catching the mouse. So only trust mouseleave while
    // click-through; while catching, main's leave-watch decides (below).
    const onDomLeave = () => {
      if (clickThroughRef.current) onLeave()
    }
    // Main saw the cursor outside the window and already restored
    // click-through; just sync our view of it.
    const onMainRestored = () => {
      clickThroughRef.current = true
      onLeave()
    }
    window.addEventListener('mousemove', onMove)
    document.documentElement.addEventListener('mouseleave', onDomLeave)
    const unsubscribe = window.botApi.onPointerLeft(onMainRestored)
    return () => {
      window.removeEventListener('mousemove', onMove)
      document.documentElement.removeEventListener('mouseleave', onDomLeave)
      unsubscribe()
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="sprite"
      width={sheet.frameWidth}
      height={sheet.frameHeight}
    />
  )
}
