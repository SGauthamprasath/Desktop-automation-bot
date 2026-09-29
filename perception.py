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
import json
import os
from datetime import datetime

# Patterns we care about for action execution later (Phase 1 executor.py
# maps directly onto these).
INTERESTING_PATTERNS = ["invoke", "toggle", "value", "expand_collapse", "selection_item"]


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
    """
    app = Application(backend="uia").connect(title_re=title_regex)
    return app.top_window()


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
            elements.append(entry)
        except Exception:
            # Some elements throw on property access (e.g. mid-render) -
            # skip rather than crash the whole extraction.
            continue

        # Recurse into children
        elements.extend(extract_element_tree(child, max_depth, child_path, _depth + 1))

    return elements


def get_screen_state(window=None):
    """
    Convenience entry point: returns the full structured tree for the
    given window (or the foreground window if none given), ready to be
    serialized into the LLM prompt.
    """
    if window is None:
        window = get_foreground_app()
    return {
        "window_title": window.window_text(),
        "elements": extract_element_tree(window),
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
