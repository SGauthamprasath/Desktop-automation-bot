"""
agent.py
--------
Phase 1 - Basic planning loop.

Deliberately simple for Phase 1: no confidence scoring, no gate, no
recovery yet (those are Phase 2 and Phase 3). This loop just proves
perception -> decision -> execution works end to end:

    perceive -> ask LLM for next action -> execute -> repeat

The LLM call and JSON-parsing here is exactly what Phase 2 will wrap
a confidence/cost gate around, so keep this loop's structure -
you'll extend it, not replace it.
"""

import json
import os
from dotenv import load_dotenv
from anthropic import Anthropic

from perception import get_screen_state, connect_to_app, get_foreground_app
from executor import execute_action

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MAX_STEPS = 15

SYSTEM_PROMPT = """You are a desktop automation agent. You are given:
1. A task instruction from the user.
2. The current UI state as a structured list of elements (from Windows UI Automation), each with a "path" you must use to reference it.

Decide the SINGLE next action to take. Respond with ONLY a JSON object, no other text, in this exact shape:

{
  "action_type": "invoke" | "set_value" | "toggle" | "click_fallback" | "done",
  "path": [<indices>],       // required unless action_type is "done"
  "value": "<text>",         // only required for set_value
  "reasoning": "<one short sentence on why this action>"
}

Use "invoke" for buttons/menu items. Use "set_value" for text fields.
Use "click_fallback" ONLY if no element has an "invoke" or "value" pattern for what you need to do.
Use "done" once the task is complete - no path needed.
"""


def _get_llm_action(task: str, screen_state: dict, history: list) -> dict:
    user_message = {
        "task": task,
        "current_screen": screen_state,
        "actions_taken_so_far": history,
    }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(user_message)}],
    )

    raw_text = response.content[0].text.strip()
    # Models sometimes wrap JSON in code fences despite instructions - strip defensively.
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    return json.loads(raw_text)


def run_task(task: str, app_title_regex: str = None):
    """
    Runs a task to completion (or until MAX_STEPS is hit).

    app_title_regex: e.g. "Notepad" - connects to a specific app.
    If omitted, uses whatever window is currently focused.
    """
    window = connect_to_app(app_title_regex) if app_title_regex else get_foreground_app()

    history = []
    for step in range(MAX_STEPS):
        state = get_screen_state(window)
        action = _get_llm_action(task, state, history)

        print(f"\n[Step {step + 1}] {action.get('reasoning', '')}")
        print(f"  -> {action}")

        if action["action_type"] == "done":
            print("\nTask marked complete by agent.")
            return {"success": True, "steps": step + 1, "history": history}

        result = execute_action(window, action)
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
    # Phase 1 milestone test: open Notepad manually first, then run this.
    result = run_task(
        task="Type 'Hello from my agent' into the document.",
        app_title_regex="Notepad",
    )
    print("\n--- RESULT ---")
    print(json.dumps({"success": result["success"], "steps": result["steps"]}, indent=2))
