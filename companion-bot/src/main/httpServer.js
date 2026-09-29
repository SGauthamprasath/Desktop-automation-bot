import http from 'http'
import { config } from '../shared/config'
import { getBotState, setBotState, stateNames } from './botState'
import { hideBubble, showBubble } from './bubble'
import { ask, getChoice, supersedePendingQuestion, waitForQuestion } from './question'

// Tiny local HTTP API so an external program (e.g. a Python agent) can drive
// the bot.
//
//   POST /state   {"state": "alert", "message": "...", "choices": ["A", "B"],
//                  "hold": true, "durationMs": 5000, "facing": "left"}
//                 Only "state" is required. "message" shows in the bubble
//                 ("message": null hides it; either one supersedes an open
//                 question). "choices" turns it into a question; the reply
//                 includes its questionId. Without "message" the bubble is
//                 left as it is.
//   GET  /state   Current state.
//   GET  /choice  Outcome of the latest question:
//                 {"questionId", "status", "choice", "choiceIndex"}.
//                 status: none | pending | answered | dismissed | superseded
//                 ?wait=N long-polls: waits up to N seconds for the answer.
//
// Why GET /choice instead of calling back a URL: the agent needs no server of
// its own, there are no failed deliveries to retry, and the bot never makes
// outgoing requests to addresses it was handed. Long-polling (?wait=) gives
// near-instant answers without a busy polling loop.
//
// Security: bound to 127.0.0.1 only. Requiring a JSON content type means a web
// page can't silently POST here (browsers must ask permission first and we
// never grant it), and checking the Host header blocks "DNS rebinding", where
// a website's domain is made to point at 127.0.0.1.

const cfg = config.http
const allowedHosts = new Set([`127.0.0.1:${cfg.port}`, `localhost:${cfg.port}`])

class HttpError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

function send(res, status, body) {
  if (res.writableEnded || res.destroyed) return
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(body))
}

function readJson(req) {
  if (!/^application\/json\b/i.test(req.headers['content-type'] || '')) {
    return Promise.reject(new HttpError(415, 'Content-Type must be application/json'))
  }
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      // Too big: stop keeping it (the rest is read and discarded so the
      // error response can still be delivered).
      if (size > cfg.maxBodyBytes) reject(new HttpError(413, `Body larger than ${cfg.maxBodyBytes} bytes`))
      else chunks.push(chunk)
    })
    req.on('end', () => {
      if (size > cfg.maxBodyBytes) return
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch {
        reject(new HttpError(400, 'Body is not valid JSON'))
      }
    })
    req.on('error', reject)
  })
}

// Check the POST /state body; returns the cleaned-up fields or throws a 400.
function validateStateBody(body) {
  const bad = (message) => {
    throw new HttpError(400, message)
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) bad('Body must be a JSON object')

  const { state, message, choices, hold, durationMs, facing } = body
  if (!stateNames.includes(state)) bad(`"state" must be one of: ${stateNames.join(', ')}`)
  if (message !== undefined && message !== null) {
    if (typeof message !== 'string') bad('"message" must be a string or null')
    if (message.length > cfg.maxMessageLength) bad(`"message" is longer than ${cfg.maxMessageLength} characters`)
  }
  if (choices !== undefined) {
    const ok =
      Array.isArray(choices) &&
      choices.length >= 1 &&
      choices.length <= cfg.maxChoices &&
      choices.every((c) => typeof c === 'string' && c.trim() && c.length <= cfg.maxChoiceLength)
    if (!ok) bad(`"choices" must be 1-${cfg.maxChoices} non-empty strings of at most ${cfg.maxChoiceLength} characters`)
  }
  if (hold !== undefined && typeof hold !== 'boolean') bad('"hold" must be true or false')
  if (durationMs !== undefined && !(Number.isFinite(durationMs) && durationMs > 0)) bad('"durationMs" must be a positive number')
  if (facing !== undefined && facing !== 'left' && facing !== 'right') bad('"facing" must be "left" or "right"')
  return { state, message, choices, hold, durationMs, facing }
}

async function postState(req, res) {
  const { state, message, choices, hold, durationMs, facing } = validateStateBody(await readJson(req))

  // A question holds its state (e.g. alert) until answered, unless told otherwise.
  const newState = setBotState(state, { hold: hold ?? Boolean(choices), durationMs, facing })

  let questionId = null
  if (choices) {
    questionId = ask({ message: message ?? '', choices, resetToIdle: true })
  } else if (message !== undefined) {
    // New text (or null = hide) replaces an open question's buttons.
    supersedePendingQuestion()
    if (message === null) hideBubble()
    else showBubble({ message }, { focus: false })
  }

  send(res, 200, { ok: true, state: newState, questionId })
}

async function getChoiceRoute(url, res) {
  const wait = Number(url.searchParams.get('wait') || 0)
  if (!(wait >= 0 && wait <= cfg.maxWaitSeconds)) {
    throw new HttpError(400, `"wait" must be between 0 and ${cfg.maxWaitSeconds} seconds`)
  }
  const current = getChoice()
  if (wait > 0 && current.status === 'pending') {
    send(res, 200, await waitForQuestion(current.questionId, wait * 1000))
  } else {
    send(res, 200, current)
  }
}

async function handle(req, res) {
  try {
    if (!allowedHosts.has(req.headers.host)) throw new HttpError(403, 'Forbidden host')
    const url = new URL(req.url, `http://${req.headers.host}`)

    if (url.pathname === '/state') {
      if (req.method === 'POST') return await postState(req, res)
      if (req.method === 'GET') return send(res, 200, getBotState())
      throw new HttpError(405, 'Use GET or POST')
    }
    if (url.pathname === '/choice') {
      if (req.method === 'GET') return await getChoiceRoute(url, res)
      throw new HttpError(405, 'Use GET')
    }
    throw new HttpError(404, 'Not found. Endpoints: POST /state, GET /state, GET /choice')
  } catch (err) {
    if (!(err instanceof HttpError)) console.error('[bot] HTTP handler error', err)
    send(res, err.status || 500, { ok: false, error: err instanceof HttpError ? err.message : 'Internal error' })
  }
}

export function startHttpServer() {
  if (!cfg.enabled) return
  const server = http.createServer(handle)
  // e.g. EADDRINUSE: the bot still works, just without external control.
  server.on('error', (err) => console.warn(`[bot] HTTP API not started: ${err.message}`))
  server.listen(cfg.port, '127.0.0.1', () => {
    console.log(`[bot] HTTP API listening on http://127.0.0.1:${cfg.port}`)
  })
}
