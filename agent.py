"""
agent.py
--------
Phase 1 - Basic planning loop.

perceive -> decide -> execute -> repeat. WHO decides is delegated to
llm_client.get_action(), which reads config.BACKEND ("local" or "cloud")
and calls the right model. This keeps the loop itself identical regardless
of backend, which matters once Phase 2's gate wraps this same call.

Actions come in two kinds:
- element actions (invoke, toggle, select, ...) run through executor.py
  against the CURRENT window's UI tree;
- tool actions (open_app, switch_window, press_keys, ...) run through
  tools.py. open_app and switch_window change which window is current, so
  a task can start with no window at all ("Open Calculator and ...").
"""

import json

import config
import tools
from perception import get_screen_state, connect_to_app, detect_popup
from executor import execute_action, ExecutionResult
from llm_client import get_action

MAX_STEPS = 15
MAX_CHANGES_LISTED = 6  # per direction - enough to describe a step, small enough for the prompt


def _screen_changes(before: dict, after: dict) -> dict:
    """
    Which element names appeared / disappeared between two perceptions.

    Small models don't reliably diff two 50-element screens themselves: in
    testing the model typed "12 + 7" into Calculator, then decided only "12"
    had gone in and typed "7" again - even though the new screen said
    "Display is 7" and "Expression is 12 +". Spelling the difference out in
    the history ("appeared": ["Display is 7", ...]) gives it that feedback
    directly. Also the seed of Phase 3's "did the action actually work?" check.
    """
    def names(state):
        return {e["name"].strip() for e in state["elements"] if e["name"].strip()}

    changes = {}
    if before.get("window_title") != after.get("window_title"):
        changes["window"] = after.get("window_title")
    appeared = sorted(names(after) - names(before))[:MAX_CHANGES_LISTED]
    gone = sorted(names(before) - names(after))[:MAX_CHANGES_LISTED]
    if appeared:
        changes["appeared"] = appeared
    if gone:
        changes["gone"] = gone
    return changes or {"note": "no visible change"}


def _alive(window) -> bool:
    try:
        return window is not None and window.exists(timeout=0)
    except Exception:
        return False


def _perceive(window) -> tuple:
    """Returns (state, popup). With no current window, the model sees an
    empty element list plus the open window titles - enough to choose
    open_app or switch_window."""
    if window is None:
        state = {"window_title": None, "elements": [], "popup_detected": None}
        popup = None
    else:
        # Detect once per step, then reuse the SAME popup reference for
        # both perception (this step's prompt) and execution - see
        # perception.detect_popup for why this exists.
        popup = detect_popup(window)
        state = get_screen_state(window, popup=popup)
    state["open_windows"] = tools.list_open_windows()
    return state, popup


def run_task(task: str, app_title_regex: str = None):
    """
    Runs a task to completion (or until MAX_STEPS is hit).

    app_title_regex: e.g. "Notepad" - starts connected to that app's window.
    If omitted, the agent starts with NO current window and is expected to
    open_app / switch_window first. (It deliberately does not fall back to
    the foreground window: when run from a terminal, that's the terminal.)
    """
    print(f"[config] backend={config.BACKEND} "
          f"model={config.LOCAL_MODEL if config.BACKEND == 'local' else config.CLOUD_MODEL}")

    window = connect_to_app(app_title_regex) if app_title_regex else None

    history = []
    previous_state = None
    for step in range(MAX_STEPS):
        if window is not None and not _alive(window):
            print("  [info] the current window has closed")
            window = None

        state, popup = _perceive(window)
        if state.get("popup_detected"):
            print(f"  [info] popup window detected: {state['popup_detected']!r}")
        if history and previous_state is not None:
            history[-1]["screen_changes"] = _screen_changes(previous_state, state)
        previous_state = state

        try:
            action = get_action(task, state, history)
        except ValueError as e:
            # Local model failed to produce a valid action even after retries -
            # a real data point for the report, not a bug to hide. Phase 2's
            # gate is the natural place to handle this more gracefully later.
            print(f"\nStopping: {e}")
            return _outcome(False, step, history, window)

        print(f"\n[Step {step + 1}] {action.get('reasoning', '')}")
        print(f"  -> {action}")

        if action["action_type"] == "done":
            print("\nTask marked complete by agent.")
            return _outcome(True, step + 1, history, window)

        if action["action_type"] in tools.TOOL_ACTIONS:
            result, new_window = tools.run_tool(action, window)
            if new_window is not None:
                window = new_window
        elif window is None:
            result = ExecutionResult(False, "none", error="no current window - use open_app or switch_window first")
        else:
            result = execute_action(window, action, popup_window=popup)
        print(f"  -> {result}")

        entry = {"action": action, "result": result.success}
        if result.detail:
            entry["note"] = result.detail  # e.g. which window opened - the model needs to know
        history.append(entry)

        if not result.success:
            # Phase 1 has no recovery yet - just log and stop.
            # Phase 3 replaces this with detection + retry/rollback.
            print(f"\nAction failed, stopping (Phase 3 will handle recovery): {result.error}")
            return _outcome(False, step + 1, history, window)

    print("\nMax steps reached without completion.")
    return _outcome(False, MAX_STEPS, history, window)


def _outcome(success, steps, history, window):
    try:
        final_window = window.window_text() if _alive(window) else None
    except Exception:
        final_window = None
    return {"success": success, "steps": steps, "history": history, "final_window": final_window}


if __name__ == "__main__":
    # Phase 1 milestone test: open test_scratch.txt in Notepad first, then run this.
    # Set AGENT_BACKEND=cloud in .env to compare against the Anthropic API instead.
    result = run_task(
        task="Type 'Hello from my agent' into the document.",
        app_title_regex="Notepad",
    )
    print("\n--- RESULT ---")
    print(json.dumps({"success": result["success"], "steps": result["steps"]}, indent=2))
