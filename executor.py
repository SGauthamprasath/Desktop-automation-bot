"""
executor.py
-----------
Phase 1 - Execution module.

Key design point from our discussion: actions are performed through
UIA's native patterns (Invoke, SetValue, Toggle) wherever possible,
NOT simulated mouse/keyboard input. This is what lets the agent act
without stealing your physical mouse cursor or keyboard focus - the
side-by-side requirement.

Simulated input (click_input / type_keys) is kept only as an explicit
fallback for elements that don't expose a usable pattern.
"""

from pywinauto.findwindows import ElementNotFoundError


class ExecutionResult:
    def __init__(self, success, method_used, error=None):
        self.success = success
        self.method_used = method_used  # "uia_pattern" or "input_simulation"
        self.error = error

    def __repr__(self):
        status = "OK" if self.success else f"FAILED ({self.error})"
        return f"<ExecutionResult {status} via {self.method_used}>"


def _locate_by_path(window, path):
    """
    Re-locates the exact element a given perception snapshot pointed to,
    by walking the same child-index path used in perception.py.
    """
    node = window
    for index in path:
        node = node.children()[index]
    return node


def execute_action(window, action: dict) -> ExecutionResult:
    """
    Executes a single action decided by the planning loop.

    Expected `action` shape (this is also what the LLM is prompted to
    output in agent.py):
        {
            "path": [2, 0, 1],       # element path from perception.py
            "action_type": "invoke" | "set_value" | "toggle" | "click_fallback",
            "value": "text to type"  # only needed for set_value
        }
    """
    try:
        element = _locate_by_path(window, action["path"])
    except (IndexError, ElementNotFoundError) as e:
        return ExecutionResult(False, "locate", error=f"element not found: {e}")

    action_type = action["action_type"]

    try:
        if action_type == "invoke":
            # Native UIA click-equivalent - no real cursor movement.
            element.invoke()
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "set_value":
            # Native UIA text-fill - no real keystrokes sent.
            element.set_edit_text(action.get("value", ""))
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "toggle":
            element.toggle()
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "click_fallback":
            # Last resort: this DOES move the real mouse cursor and
            # will visibly interrupt whatever you're doing. Phase 2's
            # gate should treat this action type as high-cost by
            # default, since it can't run silently alongside you.
            element.click_input()
            return ExecutionResult(True, "input_simulation")

        else:
            return ExecutionResult(False, "none", error=f"unknown action_type: {action_type}")

    except Exception as e:
        # Pattern not supported on this element, or the app rejected it -
        # this is exactly the kind of failure Phase 3's recovery module
        # needs to catch. For now, surface it cleanly.
        return ExecutionResult(False, "uia_pattern", error=str(e))
