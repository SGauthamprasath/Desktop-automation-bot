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
from llm_client import get_action, make_plan, check_done

MAX_STEPS = 15
# Phase 3 recovery limits. A failed action goes back to the model with its
# error instead of ending the task, but 3 failures in a row means it is stuck.
MAX_CONSECUTIVE_FAILURES = 3
# After this many rejected "done" claims, the next one stands - the done check
# is itself a 7B model and can be wrong; the benchmark still verifies.
MAX_DONE_REJECTIONS = 2
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
    changed = _state_changes(before, after)[:MAX_CHANGES_LISTED]
    if changed:
        changes["changed"] = changed
    return changes or {"note": "no visible change"}


STATE_WORDS = {"toggled": ("on", "off"), "expanded": ("expanded", "collapsed"), "selected": ("selected", "not selected")}


def _state_changes(before: dict, after: dict) -> list:
    """
    Elements whose state changed while their name stayed the same. Comparing
    names alone reported "no visible change" after every successful set_value
    or toggle - the name doesn't change, only the content or the on/off state -
    which invited the model to redo work that had already landed.
    """
    def by_name(state):
        return {(e["control_type"], e["name"].strip()): e for e in state["elements"] if e["name"].strip()}

    old, new = by_name(before), by_name(after)
    changed = []
    for key in old.keys() & new.keys():
        name = key[1]
        for field, (yes, no) in STATE_WORDS.items():
            if field in old[key] and field in new[key] and old[key][field] != new[key][field]:
                changed.append(f'"{name}" now {yes if new[key][field] else no}')
        if "value" in old[key] and "value" in new[key] and old[key]["value"] != new[key]["value"]:
            text = new[key]["value"].replace("\r\n", "\n").replace("\r", "\n")
            changed.append(f'"{name}" now contains {json.dumps(text[:60], ensure_ascii=False)}')
    return sorted(changed)


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
    plan = None
    touched = set()  # handles of windows the agent acted in - the runner's cleanup looks there
    failures_in_a_row = 0
    done_rejections = 0
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

        if step == 0 and config.USE_PLANNER:
            plan = make_plan(task, state)
            if plan:
                print("[plan] " + " -> ".join(plan["steps"]) + f"  | done when: {plan['done_when']}")
            else:
                print("[plan] no valid plan - continuing without one")

        try:
            action = get_action(task, state, history, plan)
        except ValueError as e:
            # Local model failed to produce a valid action even after retries -
            # a real data point for the report, not a bug to hide. Phase 2's
            # gate is the natural place to handle this more gracefully later.
            print(f"\nStopping: {e}")
            return _outcome(False, step, history, window, plan, touched)

        print(f"\n[Step {step + 1}] {action.get('reasoning', '')}")
        print(f"  -> {action}")

        if action["action_type"] == "done":
            if config.CHECK_DONE and done_rejections < MAX_DONE_REJECTIONS:
                verdict = check_done(task, state, history, plan)
                if verdict and not verdict["complete"]:
                    done_rejections += 1
                    missing = verdict["missing"] or "the screen does not show the task finished"
                    print(f"  [done check] rejected ({done_rejections}/{MAX_DONE_REJECTIONS}): {missing}"
                          f"  | evidence: {verdict['evidence']!r}")
                    history.append({"action": action, "result": False, "note": f"not finished yet: {missing}"})
                    continue
                if verdict:
                    print(f"  [done check] confirmed | evidence: {verdict['evidence']!r}")
            print("\nTask marked complete by agent.")
            return _outcome(True, step + 1, history, window, plan, touched)

        if action["action_type"] in tools.TOOL_ACTIONS:
            result, new_window = tools.run_tool(action, window)
            if new_window is not None:
                window = new_window
        elif window is None:
            result = ExecutionResult(False, "none", error="no current window - use open_app or switch_window first")
        else:
            result = execute_action(window, action, popup_window=popup)
        print(f"  -> {result}")
        if window is not None:
            try:
                touched.add(window.element_info.handle)
            except Exception:
                pass

        entry = {"action": action, "result": result.success}
        if result.detail:
            entry["note"] = result.detail  # e.g. which window opened - the model needs to know
        if not result.success and result.error:
            entry["note"] = result.error  # why it failed - what the model needs to recover
        history.append(entry)

        if result.success:
            failures_in_a_row = 0
        elif result.method_used == "blocked":
            # A safety refusal ends the task: retrying would mean hunting for
            # another way to do what was just refused.
            print(f"\nAction refused, stopping: {result.error}")
            return _outcome(False, step + 1, history, window, plan, touched)
        else:
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nStopping: {failures_in_a_row} failed actions in a row (last: {result.error})")
                return _outcome(False, step + 1, history, window, plan, touched)
            print(f"  [recover] action failed ({failures_in_a_row}/{MAX_CONSECUTIVE_FAILURES}) - "
                  f"the model sees the error and tries again")

    print("\nMax steps reached without completion.")
    return _outcome(False, MAX_STEPS, history, window, plan, touched)


def _outcome(success, steps, history, window, plan=None, touched=()):
    try:
        final_window = window.window_text() if _alive(window) else None
    except Exception:
        final_window = None
    return {"success": success, "steps": steps, "history": history,
            "final_window": final_window, "plan": plan, "touched_windows": sorted(touched)}


if __name__ == "__main__":
    # Phase 1 milestone test: open test_scratch.txt in Notepad first, then run this.
    # Set AGENT_BACKEND=cloud in .env to compare against the Anthropic API instead.
    result = run_task(
        task="Type 'Hello from my agent' into the document.",
        app_title_regex="Notepad",
    )
    print("\n--- RESULT ---")
    print(json.dumps({"success": result["success"], "steps": result["steps"]}, indent=2))
