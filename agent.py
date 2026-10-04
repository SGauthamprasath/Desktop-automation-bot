"""
agent.py
--------
Phase 1 - Basic planning loop.

Unchanged in structure from before: perceive -> decide -> execute -> repeat.
The only change is WHO decides - that's now delegated to llm_client.get_action(),
which reads config.BACKEND ("local" or "cloud") and calls the right model.
This keeps the loop itself identical regardless of backend, which matters
once Phase 2's gate wraps this same call.
"""

import json

import config
from perception import get_screen_state, connect_to_app, get_foreground_app, detect_popup
from executor import execute_action
from llm_client import get_action

MAX_STEPS = 15


def run_task(task: str, app_title_regex: str = None):
    """
    Runs a task to completion (or until MAX_STEPS is hit).

    app_title_regex: e.g. "Notepad" - connects to a specific app.
    If omitted, uses whatever window is currently focused.
    """
    print(f"[config] backend={config.BACKEND} "
          f"model={config.LOCAL_MODEL if config.BACKEND == 'local' else config.CLOUD_MODEL}")

    window = connect_to_app(app_title_regex) if app_title_regex else get_foreground_app()

    history = []
    for step in range(MAX_STEPS):
        # Detect once per step, then reuse the SAME popup reference for
        # both perception (this step's prompt) and execution (below) -
        # see perception.detect_popup for why this exists.
        popup = detect_popup(window)
        state = get_screen_state(window, popup=popup)
        if state.get("popup_detected"):
            print(f"  [info] popup window detected: {state['popup_detected']!r}")

        try:
            action = get_action(task, state, history)
        except ValueError as e:
            # Local model failed to produce valid JSON even after retries -
            # this is a real data point for your report (local models need
            # more JSON-reliability scaffolding than cloud models), not a bug
            # to hide. Phase 2's gate is also the natural place to handle
            # this more gracefully later.
            print(f"\nStopping: {e}")
            return {"success": False, "steps": step, "history": history}

        print(f"\n[Step {step + 1}] {action.get('reasoning', '')}")
        print(f"  -> {action}")

        if action["action_type"] == "done":
            print("\nTask marked complete by agent.")
            return {"success": True, "steps": step + 1, "history": history}

        result = execute_action(window, action, popup_window=popup)
        print(f"  -> {result}")

        history.append({"action": action, "result": result.success})

        if not result.success:
            # Phase 1 has no recovery yet - just log and stop.
            # Phase 3 replaces this with detection + retry/rollback.
            print(f"\nAction failed, stopping (Phase 3 will handle recovery): {result.error}")
            return {"success": False, "steps": step + 1, "history": history}

    print("\nMax steps reached without completion.")
    return {"success": False, "steps": MAX_STEPS, "history": history}


if __name__ == "__main__":
    # Phase 1 milestone test: open test_scratch.txt in Notepad first, then run this.
    # Set AGENT_BACKEND=cloud in .env to compare against the Anthropic API instead.
    result = run_task(
        task="Type 'Hello from my agent' into the document.",
        app_title_regex="Notepad",
    )
    print("\n--- RESULT ---")
    print(json.dumps({"success": result["success"], "steps": result["steps"]}, indent=2))