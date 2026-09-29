import { EventEmitter } from 'events'
import { getBotState, setBotState } from './botState'
import { hideBubble, onBubbleChoice, onBubbleClosedByUser, showBubble } from './bubble'

// "Ask the user" with clickable choices, shared by the demo and the HTTP API.
//
// Only one question is live at a time. Each has an id and ends up in one of:
//   answered   - the user clicked a choice
//   dismissed  - the user closed the bubble (or hid the bot) instead
//   superseded - a newer question replaced it before it was answered

const events = new EventEmitter()
let lastId = 0
let question = null // the most recent question, pending or finished

function snapshot(q) {
  if (!q) return { questionId: null, status: 'none', choice: null, choiceIndex: null }
  return { questionId: q.id, status: q.status, choice: q.choice, choiceIndex: q.choiceIndex }
}

// Show a question in the bubble; returns its id.
// resetToIdle: once answered/dismissed, return the bot to idle if it's still
// held in the state it was asked in (so an agent that never follows up
// doesn't leave the bot stuck on "alert").
export function ask({ message, choices, resetToIdle = false }) {
  if (question && question.status === 'pending') finish('superseded')
  question = {
    id: ++lastId,
    choices,
    askedInState: getBotState().name,
    resetToIdle,
    status: 'pending',
    choice: null,
    choiceIndex: null
  }
  // focus: false - don't steal the keyboard from whatever the user is doing.
  showBubble({ message, choices, questionId: question.id }, { focus: false })
  return question.id
}

// The bubble is being given other content (or hidden), so a pending question's
// buttons are gone: mark it superseded so nobody waits on it forever.
export function supersedePendingQuestion() {
  if (question && question.status === 'pending') finish('superseded')
}

export function getChoice() {
  return snapshot(question)
}

// Resolves with the outcome of question `id` once it is no longer pending,
// or with its current (pending) snapshot after timeoutMs.
export function waitForQuestion(id, timeoutMs = Infinity) {
  return new Promise((resolve) => {
    // Only the latest question is kept; older ids are already superseded.
    if (!question || question.id !== id) {
      return resolve({ ...snapshot(null), questionId: id, status: 'superseded' })
    }
    if (question.status !== 'pending') return resolve(snapshot(question))
    const timer = Number.isFinite(timeoutMs) ? setTimeout(() => done(snapshot(question)), timeoutMs) : null
    function onFinished(result) {
      if (result.questionId === id) done(result)
    }
    function done(result) {
      clearTimeout(timer)
      events.off('finished', onFinished)
      resolve(result)
    }
    events.on('finished', onFinished)
  })
}

function finish(status, index = null) {
  const q = question
  q.status = status
  if (index !== null) {
    q.choiceIndex = index
    q.choice = q.choices[index]
  }
  if (status !== 'superseded' && q.resetToIdle) {
    const state = getBotState()
    if (state.hold && state.name === q.askedInState) setBotState('idle')
  }
  events.emit('finished', snapshot(q))
}

onBubbleChoice(({ questionId, index }) => {
  if (!question || question.id !== questionId || question.status !== 'pending') return
  hideBubble() // the question is resolved; the next state brings its own message
  finish('answered', index)
})

onBubbleClosedByUser((content) => {
  if (question && question.status === 'pending' && content.questionId === question.id) {
    finish('dismissed')
  }
})
