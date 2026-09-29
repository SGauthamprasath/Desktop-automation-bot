// All tunable values live here, shared by the main process and the renderer.
//
// Sprite geometry (frame size, columns, rows, frame counts) is deliberately NOT
// here: it is read from assets/bot_spritesheet.json so the sheet can be swapped
// without touching code.

export const config = {
  // Animation speed per state, in frames per second.
  fps: {
    idle: 6,
    walk: 10,
    thinking: 6,
    alert: 8,
    talking: 8
  },
  // Fallback fps for any state in the sprite JSON that has no entry above.
  defaultFps: 6,

  // Temporary states: ms before returning to idle automatically (unless the
  // caller passes { hold: true }). States not listed stay until changed.
  stateTimeouts: {
    alert: 4000,
    talking: 3000
  },

  // Dev global shortcuts: <modifier>+1..5 trigger these states in order.
  // Never use bare number keys here: global shortcuts swallow the key in
  // every app. Note: on some layouts (e.g. UK) Ctrl+Alt acts as AltGr, and
  // AltGr+4 types "€"; use 'Control+Alt+Shift' if that matters to you.
  shortcutModifier: 'Control+Alt',
  shortcutStates: ['idle', 'walk', 'thinking', 'alert', 'talking'],

  // Roaming while idle. Distances in DIP, times in ms.
  wander: {
    enabledAtStart: true,
    pauseMinMs: 2000, // rest between walks
    pauseMaxMs: 6000,
    moveMinMs: 1000, // walk duration is distance / speed, clamped to this range
    moveMaxMs: 3000,
    speed: 180, // DIP per second
    minHop: 60, // don't bother walking shorter than this
    maxHop: 450, // each walk ends within this distance of the start (per axis)
    tickMs: 16 // window position update interval (~60 fps)
  },

  // Pointer movement (DIP) before a press on the bot counts as a drag
  // rather than a click.
  dragThresholdPx: 4,
  dragTickMs: 16, // window follows the cursor at ~60 fps while dragging

  // Speech bubble window.
  bubble: {
    width: 260, // DIP; height fits the content
    maxHeight: 400,
    gap: 0, // DIP between bubble tail and bot window
    tailInset: 18, // keep the tail at least this far from the bubble's side edges
    greeting: "Hi! I'm your desktop buddy. I can't do much yet, but you can say hello.",
    // Canned replies until a real agent is connected, used in turn.
    cannedReplies: [
      "Nice to hear from you! I'm only a visualization for now, but my agent brain is on its way.",
      "Got it. When I'm hooked up to a real agent, I'll actually do something with that.",
      "Interesting! I'll pretend I understood that perfectly.",
      'Beep boop. Message received, loud and clear.'
    ],
    maxInputLength: 500
  },

  // Tray "Play demo": the ask-vs-act sequence.
  demo: {
    thinkingMs: 3000,
    question: "I found two 'Submit' buttons. Which one did you mean?",
    choices: ['The one in the form', 'The one in the popup'],
    afterChoiceThinkingMs: 2000,
    doneMessage: 'Done. That action was reversible, so I went ahead without asking.',
    talkingMs: 4000
  },

  // Local HTTP control API (see src/main/httpServer.js). Always bound to
  // 127.0.0.1, so only programs on this computer can reach it.
  http: {
    enabled: true,
    port: 7331,
    maxBodyBytes: 16 * 1024,
    maxMessageLength: 1000,
    maxChoices: 6,
    maxChoiceLength: 80,
    maxWaitSeconds: 60 // upper limit for GET /choice?wait=N long-polling
  },

  // Gap (in DIP) between the bot and the bottom-right corner of the work area
  // when the app starts.
  startMargin: 24,

  // Hit-testing: a sprite pixel counts as "the bot" when its alpha is above
  // this value (0-255). Keeps faint anti-aliased edges click-through.
  alphaThreshold: 32,
  // Used only if the alpha read fails: a circle centred on the frame, with this
  // radius as a fraction of the frame width.
  fallbackHitRadius: 0.4,
  // While the bot is catching the mouse, how often (ms) main checks that the
  // cursor is still inside the window (see startLeaveWatch in src/main).
  leaveCheckMs: 100
}
