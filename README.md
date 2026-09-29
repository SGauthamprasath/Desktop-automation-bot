# Desktop Agent — Phase 0 & 1

This must run on **Windows** — `pywinauto` and UI Automation are Windows-only.
Copy this whole folder to your Windows machine before doing anything else.

## Phase 0 — Setup

1. Install Python 3.10+ on Windows if you don't have it.
2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and add your Anthropic API key:
   ```
   copy .env.example .env
   ```
   Then edit `.env` and paste your key in.

## Phase 0 milestone check — perception only

Before touching the LLM loop, confirm perception works on its own:

1. Open **`test_scratch.txt`** (on your Desktop) in Notepad — always use
   this scratch file for milestone checks, never a real personal
   document. The agent has no safety gate yet, so anything perception
   or later executor code touches should be safe to lose.
2. Run:
   ```
   python perception.py
   ```
3. `perception.py` connects to Notepad explicitly by window title, so it
   doesn't matter which window has focus. You should see a JSON dump of
   Notepad's UI elements (menu items, the text edit area, etc.) and a
   count at the bottom like `...total elements found: 47`.

If this prints a non-empty tree, your perception layer is working — this
is the real Phase 0 milestone, everything else depends on it.

## Phase 1 milestone check — full loop

1. Keep Notepad open (empty document).
2. Run:
   ```
   python agent.py
   ```
3. Watch it: perceive the screen → decide an action → type text into
   Notepad → mark itself done. Each step prints what it decided and
   whether execution succeeded.

## What each file does

- `perception.py` — walks the UIA tree, returns structured JSON
  (no screenshots). This is what the LLM "sees."
- `executor.py` — performs actions via UIA `Invoke`/`SetValue`/
  `Toggle` patterns first; `click_fallback` (real mouse/keyboard) only
  when nothing else works.
- `agent.py` — the loop: perceive → ask LLM for next action →
  execute → repeat. Deliberately has **no confidence gate and no
  recovery yet** — that's Phase 2 and Phase 3, built on top of this
  same loop structure.

## Known rough edges to expect (normal at this stage)

- Some elements throw on property access — `perception.py` already
  catches and skips these, but you may see fewer elements than expected
  in complex apps. Fine for now; your target apps for the benchmark
  should be relatively simple ones anyway.
- `click_fallback` will visibly move your real cursor — expected, and
  exactly the case Phase 2's cost model should flag as high-cost.
- If `set_edit_text` fails on some field, that element likely doesn't
  expose the `Value` pattern — this is a real signal to log for your
  Phase 2 cost/pattern-availability analysis, not a bug to hide.

## Next: Phase 2

Once both milestone checks above pass reliably on your 3–4 target apps,
move to building the confidence-scoring + ask-vs-act gate around the
`_get_llm_action` call in `agent.py` — that's where the gate slots in.
