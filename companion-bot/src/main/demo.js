import { EventEmitter } from 'events'
import { config } from '../shared/config'
import { onBotStateChange, setBotState } from './botState'
import { hideBubble, showBubble } from './bubble'
import { ask, waitForQuestion } from './question'

// Scripted demo of the agent's ask-vs-act behaviour:
//   think -> hit an ambiguity, ask (alert + choices) -> user picks ->
//   think -> act, and explain that it was reversible so no need to ask.
//
// If anything else takes over the bot mid-demo (a shortcut, the HTTP API,
// the user closing the question), the demo quietly stops.

const cfg = config.demo
const events = new EventEmitter()
let running = false
let interrupted = false
let selfChange = false

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export function isDemoRunning() {
  return running
}

export function onDemoChange(listener) {
  events.on('change', listener)
}

// setBotState, marked as ours so the interruption check ignores it.
function demoState(name, options) {
  selfChange = true
  setBotState(name, options)
  selfChange = false
}

export async function playDemo() {
  if (running) return
  running = true
  interrupted = false
  events.emit('change', true)
  const offState = onBotStateChange(() => {
    if (!selfChange) interrupted = true
  })

  try {
    // a. think
    hideBubble()
    demoState('thinking')
    await sleep(cfg.thinkingMs)
    if (interrupted) return

    // b. ambiguity: ask instead of guessing. Held so the alert doesn't
    //    time out while waiting for the user.
    demoState('alert', { hold: true })
    const id = ask({ message: cfg.question, choices: cfg.choices })
    const result = await waitForQuestion(id)
    if (result.status !== 'answered') {
      // Dismissed or replaced: release the held alert unless someone else
      // already set a new state.
      if (!interrupted && result.status === 'dismissed') demoState('idle')
      return
    }
    if (interrupted) return

    // c. act on the answer, then explain why it didn't ask about the rest.
    demoState('thinking')
    await sleep(cfg.afterChoiceThinkingMs)
    if (interrupted) return
    demoState('talking', { durationMs: cfg.talkingMs }) // returns to idle by itself
    showBubble({ message: cfg.doneMessage }, { focus: false })
  } finally {
    offState()
    running = false
    events.emit('change', false)
  }
}
