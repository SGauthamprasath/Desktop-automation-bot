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

# Window-chrome controls that should never be chosen by ordinary task
# execution - added after a real near-miss in testing where the agent
# repeatedly clicked Close/Minimize/Maximize on Notepad's title bar
# while confused. This is a blunt, temporary safety net standing in for
# Phase 2's real cost/reversibility gate, which doesn't exist yet.
DANGEROUS_NAMES = {"close", "minimize", "minimise", "maximize", "maximise", "restore"}


class ExecutionResult:
    def __init__(self, success, method_used, error=None):
        self.success = success
        self.method_used = method_used  # "uia_pattern" or "input_simulation"
        self.error = error

    def __repr__(self):
        status = "OK" if self.success else f"FAILED ({self.error})"
        return f"<ExecutionResult {status} via {self.method_used}>"


def _locate_by_path(window, path, popup_window=None):
    """
    Re-locates the exact element a given perception snapshot pointed to.

    A path whose first index is >= the main window's own child count
    refers to a popup element (see perception.get_screen_state) - that
    index range was deliberately assigned to mean "look in the popup
    window instead". Everything else resolves against the main window
    as before.
    """
    try:
        main_child_count = len(window.children())
    except Exception:
        main_child_count = 0

    if path and path[0] >= main_child_count and popup_window is not None:
        node = popup_window
        for index in path[1:]:
            node = node.children()[index]
        return node

    node = window
    for index in path:
        node = node.children()[index]
    return node


def execute_action(window, action: dict, popup_window=None) -> ExecutionResult:
    """
    Executes a single action decided by the planning loop.

    Expected `action` shape (this is also what the LLM is prompted to
    output in agent.py):
        {
            "path": [2, 0, 1],       # element path from perception.py
            "action_type": "invoke" | "set_value" | "toggle" | "click_fallback",
            "value": "text to type"  # only needed for set_value
        }

    popup_window: the same object agent.py got from perception.detect_popup()
    for this step, if any - passed through so a path pointing at a popup
    element (see _locate_by_path) can actually be resolved.
    """
    try:
        element = _locate_by_path(window, action["path"], popup_window=popup_window)
    except (IndexError, ElementNotFoundError) as e:
        return ExecutionResult(False, "locate", error=f"element not found: {type(e).__name__}: {e}")

    # Last-resort safety net (see DANGEROUS_NAMES above) - belongs here
    # too, not just as a filter on what the LLM sees, in case a stale
    # path or a future change in llm_client ever lets one through.
    try:
        element_name = (element.element_info.name or "").strip().lower()
    except Exception:
        element_name = ""
    if action["action_type"] in ("invoke", "click_fallback") and element_name in DANGEROUS_NAMES:
        return ExecutionResult(False, "blocked", error=f"refused: '{element_name}' is on the window-chrome denylist")

    action_type = action["action_type"]

    # Pre-check the pattern actually exists before attempting it, instead
    # of relying solely on catching NoPatternInterfaceError below - that
    # exception carries no message text, which made two real failures
    # this round genuinely unreadable (just "(no message)").
    pattern_attr = {"invoke": "iface_invoke", "toggle": "iface_toggle", "set_value": "iface_value"}.get(action_type)
    if pattern_attr:
        # hasattr() is NOT safe here: pywinauto's iface_* are lazy properties
        # whose getter calls get_elem_interface, which raises
        # NoPatternInterfaceError (not AttributeError) when the pattern is
        # absent - and can even propagate a comtypes ValueError("NULL COM
        # pointer access"). hasattr only swallows AttributeError, so the raw
        # exception escaped and crashed the runner (seen in the
        # notepad_toggle_wordwrap run). Probe with getattr + except instead.
        try:
            getattr(element, pattern_attr)
        except Exception:
            return ExecutionResult(
                False, "pattern_check",
                error=f"element '{element_name}' does not support the '{action_type}' pattern "
                      f"(perception may have mis-detected this, or the wrong element was targeted)"
            )

    try:
        if action_type == "invoke":
            # Native UIA click-equivalent - no real cursor movement.
            element.invoke()
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "set_value":
            if hasattr(element, "set_edit_text"):
                # Classic Win32 Edit control - pywinauto's convenience method works.
                element.set_edit_text(action.get("value", ""))
            else:
                # Not classified as EditWrapper (common with modern WinUI/UWP
                # apps, e.g. Windows 11's redesigned Notepad) - fall back to
                # the raw UIA Value pattern interface directly. This is the
                # same pattern perception.py's _supported_patterns already
                # checks for, so execution stays consistent with what
                # perception detected.
                element.iface_value.SetValue(action.get("value", ""))
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
        # needs to catch. Include the exception TYPE, not just str(e) -
        # some UIA/COM exceptions (e.g. invoking a disabled control)
        # carry no message text at all, which showed up as a blank,
        # unhelpful error in testing.
        message = str(e) or "(no message)"
        return ExecutionResult(False, "uia_pattern", error=f"{type(e).__name__}: {message}")