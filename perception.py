"""
perception.py
--------------
Phase 1 - Perception module.

Instead of screenshots, this reads the Windows UI Automation (UIA) tree
directly: element names, control types, bounding boxes, and which
UIA patterns (Invoke, Value, Toggle) each element supports.

This structured tree is what gets sent to the LLM as "what's on screen"
context, and what the gate (Phase 2) will later score confidence against.

Milestone check: always run this against test_scratch.txt open in
Notepad (never a real personal document) - this module has no safety
gate yet, so anything it connects to should be safe to touch.
"""

from pywinauto import Desktop
from pywinauto.application import Application
import ctypes
import json
import os
from datetime import datetime

# Patterns we care about for action execution later (Phase 1 executor.py
# maps directly onto these).
INTERESTING_PATTERNS = ["invoke", "toggle", "value", "expand_collapse", "selection_item", "scroll"]

GW_OWNER = 4
POPUP_CLASSES = {"#32768"}  # classic Win32 menus and context menus


def get_foreground_app():
    """
    Connects to whichever window currently has focus.
    Returns a pywinauto WindowSpecification.
    """
    desktop = Desktop(backend="uia")
    fg_window = desktop.window(active_only=True)
    return fg_window


def connect_to_app(title_regex: str):
    """
    Connects to a specific running application by its window title
    (regex match). Use this when you want to target a named app
    (e.g. "Notepad") rather than whatever's currently focused.

    Uses Desktop(...).window(title_re=...) rather than
    Application.connect().top_window(). The latter connects by PROCESS
    then returns that process's highest-Z-order window - fine for
    single-window apps like Notepad, but wrong for explorer.exe, which
    owns File Explorer windows, the taskbar, AND the system tray flyout
    all in one process. Matching the window directly by title avoids
    that ambiguity entirely.
    """
    if not title_regex.startswith(".*"):
        title_regex = ".*" + title_regex
    # found_index=0: if several windows match (two Calculators, two Explorer
    # windows), take the front-most instead of raising ElementAmbiguousError.
    window = Desktop(backend="uia").window(title_re=title_regex, found_index=0)
    window.wait("exists", timeout=10)
    return window


def detect_popup(main_window):
    """
    Modern Windows apps often render an open menu or dropdown as a
    SEPARATE top-level window owned by the same process, not as a
    descendant of the main window. window.children() alone never sees
    these - confirmed in testing: invoking a menu header appeared to
    "succeed" repeatedly with no new elements ever showing up, because
    the flyout was open but invisible to perception the whole time.

    Looks for another top-level Desktop window belonging to the same
    process as main_window, with a control_type that looks like a
    popup. Returns None if nothing found (the normal case - no menu
    currently open).
    """
    try:
        main_handle = main_window.element_info.handle
        main_pid = main_window.element_info.process_id
    except Exception:
        return None

    # Persistent shell windows that happen to share a process with common
    # target apps (explorer.exe owns File Explorer windows AND the
    # taskbar) - these are never the popup we're looking for, and without
    # this exclusion the taskbar was being matched on every single step,
    # confirmed in testing (it has nothing to do with whatever menu/
    # dropdown was actually open).
    EXCLUDED_TITLES = {"Taskbar", "Program Manager"}
    POPUP_TYPES = {"Menu", "List", "Pane", "Window"}

    try:
        candidates = Desktop(backend="uia").windows()
    except Exception:
        return None

    for w in candidates:
        try:
            info = w.element_info
            if info.handle == main_handle:
                continue
            if info.process_id != main_pid:
                continue
            if w.window_text() in EXCLUDED_TITLES:
                continue
            # Same process is NOT enough: every Store/UWP app window
            # (Calculator, Settings, ...) lives in ApplicationFrameHost, so a
            # second Calculator was being merged in as this one's "popup" -
            # confirmed in testing. A real popup is owned by the main window
            # (dialogs, flyouts) or is a classic Win32 menu window.
            owned = ctypes.windll.user32.GetWindow(info.handle, GW_OWNER) == main_handle
            if info.control_type in POPUP_TYPES and (owned or info.class_name in POPUP_CLASSES):
                return w
        except Exception:
            continue
    return None


def _supported_patterns(element):
    """Checks which UIA patterns an element actually supports."""
    supported = []
    for pattern in INTERESTING_PATTERNS:
        try:
            # In pywinauto 0.6.8 the iface_* properties live on the wrapper
            # (not element_info) and raise NoPatternInterfaceError when the
            # element doesn't support that pattern.
            if getattr(element, f"iface_{pattern}", None) is not None:
                supported.append(pattern)
        except Exception:
            # Not all elements expose every pattern - that's expected, skip it.
            continue
    return supported


def _pattern_state(element, patterns):
    """
    Current state of stateful patterns, so the model can tell a menu that's
    already open from a closed one, or a setting that's already on from an
    off one. Without this, testing showed the model re-invoking an open
    menu (which closes it) in a loop, and it had no way to know whether an
    "enable X" task was already satisfied.

    Only included when the element supports the pattern; values are plain
    booleans. Indeterminate/partial/leaf states are left out rather than
    guessed.
    """
    state = {}
    if "toggle" in patterns:
        try:
            value = element.iface_toggle.CurrentToggleState  # 0 off, 1 on, 2 indeterminate
            if value in (0, 1):
                state["toggled"] = bool(value)
        except Exception:
            pass
    if "expand_collapse" in patterns:
        try:
            value = element.iface_expand_collapse.CurrentExpandCollapseState  # 0 collapsed, 1 expanded, 2 partial, 3 leaf
            if value in (0, 1):
                state["expanded"] = bool(value)
        except Exception:
            pass
    if "selection_item" in patterns:
        try:
            state["selected"] = bool(element.iface_selection_item.CurrentIsSelected)
        except Exception:
            pass
    return state


def extract_element_tree(window, max_depth: int = 6, _path=None, _depth=0):
    """
    Recursively walks the UIA tree starting at `window` and returns a
    structured, JSON-serializable list of elements.

    Each element gets a stable `path` (a list of child-indices from the
    root) - this path is what executor.py uses later to re-locate the
    exact same element for an action, without relying on screen
    coordinates.
    """
    if _path is None:
        _path = []
    if _depth > max_depth:
        return []

    elements = []
    try:
        children = window.children()
    except Exception:
        return []

    for i, child in enumerate(children):
        child_path = _path + [i]
        try:
            info = child.element_info
            rect = info.rectangle
            entry = {
                "path": child_path,
                "control_type": info.control_type,
                "name": info.name,
                "auto_id": info.automation_id,
                "enabled": child.is_enabled(),
                "visible": child.is_visible(),
                "bounding_box": {
                    "left": rect.left, "top": rect.top,
                    "right": rect.right, "bottom": rect.bottom,
                },
                "patterns": _supported_patterns(child),
            }
            entry.update(_pattern_state(child, entry["patterns"]))
            elements.append(entry)
        except Exception:
            # Some elements throw on property access (e.g. mid-render) -
            # skip rather than crash the whole extraction.
            continue

        # Recurse into children
        elements.extend(extract_element_tree(child, max_depth, child_path, _depth + 1))

    return elements


def get_screen_state(window=None, popup=None):
    """
    Convenience entry point: returns the full structured tree for the
    given window (or the foreground window if none given), ready to be
    serialized into the LLM prompt.

    popup: pass a window object from detect_popup() to merge its
    elements in too (e.g. an open menu's items). If omitted, this
    auto-detects a popup itself - fine for standalone/manual use, but
    agent.py's loop should detect once per step and pass it explicitly
    so execute_action() later uses the SAME popup reference, not a
    freshly re-detected one that might differ by the time it runs.

    Popup elements get paths starting at index len(window.children()) -
    i.e. treated as if they were one more top-level child after the
    main window's real children. executor.py's _locate_by_path knows
    to redirect any path starting at that index to the popup window
    instead of the main one.
    """
    if window is None:
        window = get_foreground_app()

    main_elements = extract_element_tree(window)
    all_elements = list(main_elements)
    popup_title = None

    if popup is None:
        popup = detect_popup(window)
    if popup is not None:
        try:
            popup_root_index = len(window.children())
            popup_elements = extract_element_tree(popup, _path=[popup_root_index])
            all_elements.extend(popup_elements)
            popup_title = popup.window_text()
        except Exception:
            pass

    return {
        "window_title": window.window_text(),
        "elements": all_elements,
        "popup_detected": popup_title,  # None, or the open popup's title - visible signal for debugging too
    }


SNAPSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def save_snapshot(state: dict, label: str = "snapshot"):
    """
    Persists a captured state dict to snapshots/ as both JSON and a
    human-readable .txt listing, so a run can be inspected after the
    fact instead of only read off the terminal.

    Purely additive - does not change what's captured, only saves it.
    """
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{label}_{timestamp}"

    json_path = os.path.join(SNAPSHOTS_DIR, f"{base}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    txt_path = os.path.join(SNAPSHOTS_DIR, f"{base}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        for e in state["elements"]:
            box = e["bounding_box"]
            f.write(
                f"{e['path']} {e['control_type']}: \"{e['name']}\" "
                f"| patterns: {e['patterns']} "
                f"| box: ({box['left']}, {box['top']}, {box['right']}, {box['bottom']})\n"
            )

    print(f"Snapshot JSON: {json_path}")
    print(f"Snapshot TXT:  {txt_path}")
    return json_path, txt_path


if __name__ == "__main__":
    # Quick manual test: run this with Notepad open (it does not need to
    # be the focused/foreground window - we connect to it by title
    # explicitly, so a background terminal can't get read by mistake).
    # This is your Phase 0 milestone check - if this prints a
    # non-empty element list, perception is working.
    #
    # Use test_scratch.txt (see README) as the open document, never a
    # real personal file - this script has no safety gate yet, so it
    # should never touch content you care about, even read-only.
    notepad_window = connect_to_app(".*Notepad")
    state = get_screen_state(notepad_window)
    print(json.dumps(state, indent=2)[:3000])
    print(f"\n...total elements found: {len(state['elements'])}")
    save_snapshot(state, label="notepad_test")