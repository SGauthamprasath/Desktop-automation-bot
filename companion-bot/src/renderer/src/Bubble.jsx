import { useEffect, useRef, useState } from 'react'
import { config } from '../../shared/config'

// The speech bubble's page. Renders whatever content the main process sends
// ({ message, input, choices, questionId }), reports its own height so the
// window fits it exactly,
// and draws its tail on the side facing the bot.
export default function Bubble() {
  const [content, setContent] = useState({ message: '', input: false })
  const [placement, setPlacement] = useState({ side: 'above', tailX: config.bubble.width / 2 })
  const [text, setText] = useState('')
  const rootRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    const offContent = window.bubbleApi.onContent(setContent)
    const offPlacement = window.bubbleApi.onPlacement(setPlacement)
    window.bubbleApi.getInitial().then((initial) => {
      if (!initial) return
      setContent(initial.content)
      setPlacement(initial.placement)
    })
    return () => {
      offContent()
      offPlacement()
    }
  }, [])

  // Keep the window exactly as tall as the bubble (+ tail).
  useEffect(() => {
    const root = rootRef.current
    const observer = new ResizeObserver(() => window.bubbleApi.resize(root.offsetHeight))
    observer.observe(root)
    return () => observer.disconnect()
  }, [])

  // Put the caret in the input whenever the bubble has focus: when new content
  // arrives (the window may have gained focus before React rendered), and
  // when the window is focused again later.
  useEffect(() => {
    if (content.input && document.hasFocus()) inputRef.current?.focus()
  }, [content])

  useEffect(() => {
    const onFocus = () => inputRef.current?.focus()
    const onKey = (e) => {
      if (e.key === 'Escape') window.bubbleApi.close()
    }
    window.addEventListener('focus', onFocus)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('focus', onFocus)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  const submit = (e) => {
    e.preventDefault()
    if (!text.trim()) return
    window.bubbleApi.submit(text)
    setText('')
  }

  return (
    <div
      ref={rootRef}
      className={`bubble-root ${placement.side}`}
      style={{ '--tail-x': `${placement.tailX}px` }}
    >
      <div className="bubble">
        <button className="close" aria-label="Close" onClick={() => window.bubbleApi.close()}>
          ×
        </button>
        {content.message && <p className="message">{content.message}</p>}
        {content.choices?.length > 0 && (
          <div className="choices">
            {content.choices.map((choice, i) => (
              <button key={i} onClick={() => window.bubbleApi.choose(content.questionId, i)}>
                {choice}
              </button>
            ))}
          </div>
        )}
        {content.input && (
          <form className="input-row" onSubmit={submit}>
            <input
              ref={inputRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={config.bubble.maxInputLength}
              placeholder="Say something…"
              aria-label="Message"
            />
            <button type="submit" disabled={!text.trim()}>
              Send
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
