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

import ctypes
import re
import time

from pywinauto.findwindows import ElementNotFoundError

import config

# Window-chrome controls that should never be chosen by ordinary task
# execution - added after a real near-miss in testing where the agent
# repeatedly clicked Close/Minimize/Maximize on Notepad's title bar
# while confused. This is a blunt, temporary safety net standing in for
# Phase 2's real cost/reversibility gate, which doesn't exist yet.
# "exit"/"quit" were missing: asked to "Close Notepad", the agent used
# File > Exit instead, and Notepad (holding the user's tabs) really closed.
DANGEROUS_NAMES = {"close", "exit", "quit", "minimize", "minimise", "maximize", "maximise", "restore"}
DANGEROUS_PHRASES = {"sign out", "log off", "log out", "shut down"}


def is_dangerous(name: str) -> bool:
    """
    True if a control's name STARTS with a denylisted word - so "Close",
    "Close Tab" and "Close window" are all caught (an exact-match check
    missed Notepad's per-tab "Close Tab" button), while settings that merely
    mention the word, like "Auto close brackets", are not.
    """
    words = re.findall(r"[a-z]+", (name or "").lower())
    return bool(words) and (words[0] in DANGEROUS_NAMES or " ".join(words[:2]) in DANGEROUS_PHRASES)


FLYOUT_SETTLE_SECONDS = 0.4

EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2


def _replace_text_via_edit_messages(element, text: str) -> bool:
    """
    Replaces all text in a native Edit/RichEdit control by selecting
    everything and sending EM_REPLACESEL to its window handle.

    Why not just UIA SetValue: on Windows 11 Notepad's RichEdit control,
    SetValue changes what the control displays but the app's own document
    never hears about it - the tab isn't marked modified, and the text
    silently vanished on the next re-layout (toggling Word wrap wiped it in
    testing, after the agent had already reported success). EM_REPLACESEL
    goes through the control's normal editing path, so the app sees a real
    edit (marked modified, undoable). Like SetValue it needs no focus and
    no simulated keystrokes.

    Returns False (caller falls back to UIA) when the element has no native
    handle or its window class isn't an edit control.
    """
    try:
        handle = element.element_info.handle
        if not handle:
            return False
        class_name = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(handle, class_name, 256)
        if "edit" not in class_name.value.lower():
            return False
    except Exception:
        return False

    send = ctypes.windll.user32.SendMessageW
    send(handle, EM_SETSEL, 0, -1)
    send(handle, EM_REPLACESEL, 1, ctypes.c_wchar_p(text))  # wParam=1: undoable
    return True


def _supports(element, pattern_attr: str) -> bool:
    # hasattr() is NOT safe here: pywinauto's iface_* are lazy properties
    # whose getter calls get_elem_interface, which raises
    # NoPatternInterfaceError (not AttributeError) when the pattern is
    # absent - and can even propagate a comtypes ValueError("NULL COM
    # pointer access"). hasattr only swallows AttributeError, so the raw
    # exception escaped and crashed the runner (seen in the
    # notepad_toggle_wordwrap run). Probe with getattr + except instead.
    try:
        return getattr(element, pattern_attr) is not None
    except Exception:
        return False


# Actions that amount to "click it" - all subject to the denylist.
CLICK_LIKE_ACTIONS = {"invoke", "toggle", "select", "expand", "click_fallback"}

PATTERN_ATTR_FOR_ACTION = {
    "invoke": "iface_invoke",
    "toggle": "iface_toggle",
    "set_value": "iface_value",
    "select": "iface_selection_item",
    "expand": "iface_expand_collapse",
    "collapse": "iface_expand_collapse",
}

MAX_SCROLL_ANCESTORS = 30  # far deeper than any real UI tree; just guarantees termination
NOTHING_MOVED = "nothing moved"  # result-detail prefix that llm_client's repeat check looks for

# UIA ScrollAmount values, as (horizontal, vertical) for each direction.
_NO_AMOUNT, _LARGE_DECREMENT, _LARGE_INCREMENT = 2, 0, 3
_SCROLL_AMOUNTS = {
    "down": (_NO_AMOUNT, _LARGE_INCREMENT),
    "up": (_NO_AMOUNT, _LARGE_DECREMENT),
    "right": (_LARGE_INCREMENT, _NO_AMOUNT),
    "left": (_LARGE_DECREMENT, _NO_AMOUNT),
}


def _scroll(element, direction: str) -> "ExecutionResult":
    """
    Scrolls one page in `direction` with the UIA Scroll pattern. The model
    often names an element INSIDE the scrollable area (a list item, the
    text) rather than the container itself, so this walks up to the
    nearest ancestor that actually supports Scroll.
    """
    if direction not in _SCROLL_AMOUNTS:
        return ExecutionResult(False, "none", error=f"scroll direction must be one of {sorted(_SCROLL_AMOUNTS)}, got {direction!r}")

    node = element
    for _ in range(MAX_SCROLL_ANCESTORS):
        if node is None or _supports(node, "iface_scroll"):
            break
        try:
            node = node.parent()
        except Exception:
            node = None
    if node is None or not _supports(node, "iface_scroll"):
        return ExecutionResult(False, "pattern_check",
                               error="neither this element nor any container around it supports scrolling "
                                     "(try press_keys with pagedown/pageup instead)")

    scroll = node.iface_scroll
    vertical = direction in ("up", "down")
    if not (scroll.CurrentVerticallyScrollable if vertical else scroll.CurrentHorizontallyScrollable):
        return ExecutionResult(False, "uia_pattern", error=f"the content can't scroll {direction} - it already fits")

    def position():
        return scroll.CurrentVerticalScrollPercent if vertical else scroll.CurrentHorizontalScrollPercent

    before = position()
    horizontal_amount, vertical_amount = _SCROLL_AMOUNTS[direction]
    scroll.Scroll(horizontal_amount, vertical_amount)
    after = position()
    if after == before:
        # Seen in testing: the model kept scrolling for 15 steps at 100%,
        # unable to tell nothing was moving. Say so explicitly - and
        # llm_client rejects repeating an action that moved nothing.
        return ExecutionResult(True, "uia_pattern",
                               detail=f"{NOTHING_MOVED} - already at the end in that direction ({after:.0f}%)")
    return ExecutionResult(True, "uia_pattern", detail=f"scrolled {direction}; now at {after:.0f}%")


class ExecutionResult:
    def __init__(self, success, method_used, error=None, detail=None):
        self.success = success
        # "uia_pattern", "uia_pattern+focus", "win32_edit_message",
        # "input_simulation" or "tool"
        self.method_used = method_used
        self.error = error
        self.detail = detail  # short note for the model's history, e.g. which window opened

    def __repr__(self):
        status = "OK" if self.success else f"FAILED ({self.error})"
        note = f" - {self.detail}" if self.detail else ""
        return f"<ExecutionResult {status} via {self.method_used}{note}>"


def _normalize_text(text) -> str:
    # RichEdit reports line breaks as "\r", edit controls as "\r\n".
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _state_before(element, action_type: str):
    """What the per-action check needs to know before acting."""
    if action_type == "toggle":
        try:
            return element.iface_toggle.CurrentToggleState
        except Exception:
            return None
    return None


def _postcondition_problem(element, action: dict, before):
    """
    Reads the element back after a successful action and returns a short
    description of what didn't change as expected, or None if it did (or
    if it can't be told - e.g. the element vanished because toggling a menu
    item closed its menu). invoke, scroll and the click fallback have no
    generic expected state; their effect shows up in the screen changes.
    """
    action_type = action["action_type"]
    try:
        if action_type == "set_value":
            wanted = _normalize_text(action.get("value", ""))
            actual = _normalize_text(element.iface_value.CurrentValue)
            if actual != wanted:
                return f"the field now contains {actual[:60]!r}, not {wanted[:60]!r}"
        elif action_type == "toggle" and before is not None:
            if element.iface_toggle.CurrentToggleState == before:
                return "the toggle state did not change"
        elif action_type == "select":
            if not element.iface_selection_item.CurrentIsSelected:
                return "the element is not selected"
        elif action_type in ("expand", "collapse"):
            state = element.iface_expand_collapse.CurrentExpandCollapseState  # 0 collapsed, 1 expanded
            if action_type == "expand" and state == 0:
                return "it is still collapsed"
            if action_type == "collapse" and state == 1:
                return "it is still expanded"
    except Exception:
        return None  # can't read it back any more - not evidence of failure
    return None


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
            "action_type": "invoke" | "set_value" | "toggle" | "select" |
                           "expand" | "collapse" | "scroll" | "click_fallback",
            "value": "text to type"  # set_value: the text; scroll: up/down/left/right
        }

    Tool actions that don't target an element (open_app, press_keys, ...)
    live in tools.py.

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
    if action["action_type"] in CLICK_LIKE_ACTIONS and is_dangerous(element_name):
        return ExecutionResult(False, "blocked", error=f"refused: '{element_name}' is on the window-chrome denylist")

    action_type = action["action_type"]

    # Pre-check the pattern actually exists before attempting it, instead
    # of relying solely on catching NoPatternInterfaceError below - that
    # exception carries no message text, which made two real failures
    # this round genuinely unreadable (just "(no message)").
    # (scroll is checked separately - it may act on a scrollable ancestor.)
    pattern_attr = PATTERN_ATTR_FOR_ACTION.get(action_type)
    if pattern_attr:
        if not _supports(element, pattern_attr):
            return ExecutionResult(
                False, "pattern_check",
                error=f"element '{element_name}' does not support the '{action_type}' pattern "
                      f"(perception may have mis-detected this, or the wrong element was targeted)"
            )

    before = _state_before(element, action_type)
    result = _perform(window, element, action)
    if result.success:
        problem = _postcondition_problem(element, action, before)
        if problem:
            # e.g. the Phase 1 Notepad bug: SetValue "succeeded" but the
            # text never stuck. Report it as the failure it is.
            return ExecutionResult(False, result.method_used, error=f"the action ran but {problem}")
    return result


def _perform(window, element, action: dict) -> ExecutionResult:
    action_type = action["action_type"]
    try:
        if action_type == "invoke":
            # Opens a flyout (menu, dropdown, split button)? See
            # config.FOCUS_BEFORE_EXPAND - those don't render unless the app
            # is foreground, so the next perception step would see nothing.
            opens_flyout = config.FOCUS_BEFORE_EXPAND and _supports(element, "iface_expand_collapse")
            if opens_flyout:
                window.set_focus()
            # Native UIA click-equivalent - no real cursor movement.
            element.invoke()
            if opens_flyout:
                time.sleep(FLYOUT_SETTLE_SECONDS)  # let the open animation finish before re-perceiving
                # Distinct method label so Phase 2 can count focus steals.
                return ExecutionResult(True, "uia_pattern+focus")
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "set_value":
            # None (a JSON null) would reach EM_REPLACESEL as a null pointer;
            # edit controls want "\r\n" line breaks, the model writes "\n".
            text = _normalize_text(action.get("value")).replace("\n", "\r\n")
            if _replace_text_via_edit_messages(element, text):
                return ExecutionResult(True, "win32_edit_message")
            if hasattr(element, "set_edit_text"):
                # Classic Win32 Edit control - pywinauto's convenience method works.
                element.set_edit_text(text)
            else:
                # Not classified as EditWrapper (common with modern WinUI/UWP
                # apps, e.g. Windows 11's redesigned Notepad) - fall back to
                # the raw UIA Value pattern interface directly. This is the
                # same pattern perception.py's _supported_patterns already
                # checks for, so execution stays consistent with what
                # perception detected.
                element.iface_value.SetValue(text)
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "toggle":
            if hasattr(element, "toggle"):
                element.toggle()
            else:
                # Same situation as set_value above: some wrappers (e.g.
                # MenuItemWrapper, used for checkable menu items like
                # Notepad's "Word wrap") expose the Toggle pattern but no
                # .toggle() convenience method - call the UIA interface.
                element.iface_toggle.Toggle()
            # Read the new state straight away and put it in the history:
            # toggling a menu item closes its menu, so the model can no
            # longer SEE the result. In testing it turned Word wrap on, then
            # believed it was still off and went looking in Settings.
            try:
                state = "on" if element.iface_toggle.CurrentToggleState == 1 else "off"
                return ExecutionResult(True, "uia_pattern", detail=f"{element.element_info.name!r} is now {state}")
            except Exception:
                return ExecutionResult(True, "uia_pattern")

        elif action_type == "select":
            # SelectionItem: list items, tabs, files, radio buttons. Selects
            # without "opening" - unlike invoke, which on a file or folder in
            # Explorer opens it.
            element.iface_selection_item.Select()
            return ExecutionResult(True, "uia_pattern")

        elif action_type in ("expand", "collapse"):
            opens_flyout = action_type == "expand" and config.FOCUS_BEFORE_EXPAND
            if opens_flyout:
                window.set_focus()  # see invoke above - flyouts only render in the foreground
            if action_type == "expand":
                element.iface_expand_collapse.Expand()
            else:
                element.iface_expand_collapse.Collapse()
            if opens_flyout:
                time.sleep(FLYOUT_SETTLE_SECONDS)
                return ExecutionResult(True, "uia_pattern+focus")
            return ExecutionResult(True, "uia_pattern")

        elif action_type == "scroll":
            return _scroll(element, str(action.get("value") or "down").strip().lower())

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