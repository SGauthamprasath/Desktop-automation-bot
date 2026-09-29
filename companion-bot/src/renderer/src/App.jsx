import { useEffect, useState } from 'react'
import Sprite from './Sprite'
import { useDragOrClick } from './useDragOrClick'

// Mirrors the state machine in the main process and renders it.
export default function App() {
  const [botState, setBotState] = useState({ name: 'idle', facing: 'right' })
  const { pressed, dragging, handlers } = useDragOrClick()

  useEffect(() => {
    // Subscribe first so a change arriving during the initial fetch isn't
    // lost, and don't let the (older) fetch result overwrite it.
    let pushed = false
    const unsubscribe = window.botApi.onBotState((state) => {
      pushed = true
      setBotState(state)
    })
    window.botApi.getBotState().then((state) => {
      if (!pushed) setBotState(state)
    })
    return unsubscribe
  }, [])

  return (
    <div className={dragging ? 'bot dragging' : 'bot'} {...handlers}>
      {/* While dragged, show the idle pose without changing the real state,
          so e.g. "thinking" set by an agent comes back after the drop. */}
      <Sprite
        state={dragging ? 'idle' : botState.name}
        flipped={botState.facing === 'left'}
        locked={pressed}
      />
    </div>
  )
}
