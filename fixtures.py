"""
fixtures.py
-----------
Benchmark scaffolding for runner.py - NOT used by the agent itself.

- A scratch workspace (text files, a folder of files) built fresh in the
  temp directory for every run, so tasks never touch the user's own files.
- Declarative setup / teardown steps that tasks.json can list, e.g.
      {"open_file": "{workspace}/bench_scratch.txt"}
      {"menu_toggle": {"window": "bench_scratch.txt", "menu": "View", "item": "Word wrap", "state": false}}
- Cleanup after every task: cancel dialogs and close menus left open in the
  windows the agent touched, and close windows the agent opened. In testing
  a stray File > Open dialog from one task silently broke the next ones.

Element and window lookups here are by name/title, which is fine for a
benchmark whose fixtures we control - the agent itself uses perception.
"""

import os
import re
import subprocess
import tempfile
import time

from pywinauto import Desktop

WORKSPACE = os.path.join(tempfile.gettempdir(), "desktop-agent-bench")
WAIT_SECONDS = 15
IGNORED_TITLES = {"Taskbar", "Program Manager", ""}


# ---------------------------------------------------------------- workspace

def build_workspace(files: dict) -> str:
    """files: {"relative/path.txt": "content" | {"repeat": "line {i}", "count": 300}}"""
    os.makedirs(WORKSPACE, exist_ok=True)
    for relative, content in (files or {}).items():
        path = os.path.join(WORKSPACE, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_text(content))
    return WORKSPACE


def _text(content) -> str:
    if isinstance(content, dict):
        return "\n".join(content["repeat"].format(i=i) for i in range(1, content["count"] + 1))
    return str(content)


def expand(value):
    """Replaces {workspace} in every string of a (nested) step/check spec."""
    if isinstance(value, str):
        return value.replace("{workspace}", WORKSPACE)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- lookups

def top_windows() -> list:
    return [w for w in Desktop(backend="uia").windows() if w.window_text() not in IGNORED_TITLES]


def find_window(title_regex: str, timeout: float = WAIT_SECONDS):
    """Front-most window whose title matches. For a Notepad file, the title
    only shows the SELECTED tab, so the matching tab is selected first."""
    deadline = time.time() + timeout
    while True:
        for w in top_windows():
            if re.search(title_regex, w.window_text()):
                return Desktop(backend="uia").window(handle=w.handle)
        notepad = _select_notepad_tab(title_regex)
        if notepad is not None:
            return notepad
        if time.time() > deadline:
            raise LookupError(f"no window matches {title_regex!r}")
        time.sleep(0.5)


def _select_notepad_tab(title_regex: str):
    for w in Desktop(backend="uia").windows(class_name="Notepad"):
        _leave_notepad_settings(w)
        for tab in w.descendants(control_type="TabItem"):
            if re.search(title_regex, tab.element_info.name):
                tab.iface_selection_item.Select()
                time.sleep(0.6)
                return Desktop(backend="uia").window(handle=w.handle)
    return None


def _leave_notepad_settings(w):
    # Notepad's Settings page hides the tab strip; an agent has opened it by mistake.
    if not w.descendants(control_type="TabItem"):
        back = [b for b in w.descendants(control_type="Button") if b.element_info.name == "Back"]
        if back:
            back[0].invoke()
            time.sleep(1.2)


def _matches(element, spec: dict) -> bool:
    info = element.element_info
    name = info.name or ""
    return ((("name" not in spec) or name == spec["name"])
            and (("name_contains" not in spec) or spec["name_contains"].lower() in name.lower())
            and (("automation_id" not in spec) or info.automation_id == spec["automation_id"])
            and (("control_type" not in spec) or info.control_type == spec["control_type"]))


def find_element(window, spec: dict, timeout: float = 3):
    # Retries briefly: right after the agent's last action the tree can be
    # mid-update (a check once missed Notepad's View menu this way).
    deadline = time.time() + timeout
    while True:
        for element in window.descendants():
            if _matches(element, spec):
                return element
        if time.time() > deadline:
            raise LookupError(f"no element matches {spec}")
        time.sleep(0.5)


class menu_open:
    """Opens a menu (by name) for reading/changing one of its items, then closes it."""

    def __init__(self, window, menu_name):
        self.window, self.menu_name, self.menu = window, menu_name, None

    def __enter__(self):
        if self.menu_name:
            self.menu = find_element(self.window, {"name": self.menu_name, "control_type": "MenuItem"})
            self.window.set_focus()
            time.sleep(0.3)
            self.menu.iface_expand_collapse.Expand()
            time.sleep(0.6)
        return self

    def reopen(self):
        # Toggling a menu item can close its menu.
        if self.menu is not None and self.menu.iface_expand_collapse.CurrentExpandCollapseState != 1:
            self.menu.iface_expand_collapse.Expand()
            time.sleep(0.6)

    def __exit__(self, *exc):
        try:
            if self.menu is not None and self.menu.iface_expand_collapse.CurrentExpandCollapseState == 1:
                self.menu.iface_expand_collapse.Collapse()
                time.sleep(0.3)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- setup / teardown steps

def _open_file(path):
    path = os.path.normpath(path)  # "{workspace}/x" has a forward slash
    name = re.escape(os.path.basename(path))
    try:
        find_window(name, timeout=0)
        return
    except LookupError:
        subprocess.Popen(["notepad.exe", path])
    find_window(name)


def _open_folder(path):
    # explorer.exe silently opens Documents for a path with forward slashes.
    path = os.path.normpath(path)
    title = re.escape(f"{os.path.basename(path)} - File Explorer")
    try:
        find_window(title, timeout=0)
        return
    except LookupError:
        subprocess.Popen(["explorer.exe", path])
    find_window(title)


def _close_windows(title_regex):
    for w in top_windows():
        if re.search(title_regex, w.window_text()):
            w.close()
    time.sleep(1.5)


def _open_app(name):
    import tools
    if not any(w.window_text() == name for w in top_windows()):
        tools.open_app(name)


def _element_step(arg, act):
    window = find_window(arg["window"])
    with menu_open(window, arg.get("menu")):
        act(find_element(window, arg))


def _set_text(arg):
    from executor import _replace_text_via_edit_messages
    window = find_window(arg["window"])
    element = find_element(window, arg)
    text = _text(arg["text"]).replace("\n", "\r\n")
    if not _replace_text_via_edit_messages(element, text):
        element.iface_value.SetValue(text)
    for _ in range(10):  # the editor briefly rejects UIA calls right after a large edit
        time.sleep(0.5)
        try:
            element.iface_scroll.SetScrollPercent(-1, 0)
            break
        except Exception:
            continue


def _menu_toggle(arg):
    window = find_window(arg["window"])
    with menu_open(window, arg["menu"]) as menu:
        item = find_element(window, {"name": arg["item"], "control_type": "MenuItem"})
        if bool(item.iface_toggle.CurrentToggleState) != arg["state"]:
            item.iface_toggle.Toggle()
            time.sleep(0.4)
            menu.reopen()


STEPS = {
    "open_file": _open_file,
    "open_folder": _open_folder,
    "close_windows": _close_windows,
    "open_app": _open_app,
    "invoke": lambda a: _element_step(a, lambda e: e.iface_invoke.Invoke()),
    "select": lambda a: _element_step(a, lambda e: e.iface_selection_item.Select()),
    "set_text": _set_text,
    "menu_toggle": _menu_toggle,
}


STEP_ATTEMPTS = 3


def run_steps(steps: list):
    for step in steps or []:
        (kind, arg), = step.items()
        for attempt in range(STEP_ATTEMPTS):
            try:
                STEPS[kind](expand(arg))
                break
            except Exception:
                # UIA occasionally throws transient COM errors mid-animation
                # ("An event was unable to invoke any of the subscribers").
                if attempt == STEP_ATTEMPTS - 1:
                    raise
                time.sleep(1)


# ---------------------------------------------------------------- side effects and cleanup

def snapshot_windows() -> dict:
    return {w.handle: w.window_text() for w in top_windows()}


def _dialogs(window) -> list:
    """Real dialogs only: modal child windows, or classic Win32 dialogs
    (#32770, e.g. File > Open). Store apps like Calculator have non-modal
    child "Window" elements as ordinary structure - counting those flagged
    every Calculator task as leaving a dialog open."""
    dialogs = []
    for child in window.children():
        if child.element_info.control_type != "Window":
            continue
        try:
            modal = bool(child.iface_window.CurrentIsModal)
        except Exception:
            modal = False
        if modal or child.element_info.class_name == "#32770":
            dialogs.append(child)
    return dialogs


def detect_side_effects(before: dict, touched: list, allowed_new: str = None) -> list:
    """Problems the task left behind: dialogs still open in windows the agent
    acted in, and new windows nobody asked for."""
    problems = []
    for handle in touched:
        try:
            window = Desktop(backend="uia").window(handle=handle)
            names = [d.element_info.name for d in _dialogs(window)]
            if names:
                problems.append(f"dialog left open in {window.window_text()!r}: {names}")
        except Exception:
            continue
    for handle, title in snapshot_windows().items():
        if handle not in before and not (allowed_new and re.search(allowed_new, title)):
            problems.append(f"unexpected new window {title!r}")
    return problems


def cleanup(before: dict, touched: list):
    """Restores a neutral state for the next task: cancel dialogs and collapse
    menus in touched windows, close windows the agent opened."""
    for handle in touched:
        try:
            window = Desktop(backend="uia").window(handle=handle)
            for dialog in _dialogs(window):
                cancel = [b for b in dialog.descendants(control_type="Button") if b.element_info.name == "Cancel"]
                if cancel:
                    cancel[0].invoke()
                    time.sleep(0.8)
            for item in window.descendants(control_type="MenuItem"):
                try:
                    if item.iface_expand_collapse.CurrentExpandCollapseState == 1:
                        item.iface_expand_collapse.Collapse()
                except Exception:
                    continue
            if window.element_info.class_name == "Notepad":
                _leave_notepad_settings(window)
        except Exception:
            continue
    # Only windows that are new AND that the agent acted in (e.g. one it
    # launched with open_app). A window the user opens mid-run is new too,
    # but is never closed here - it only shows up as a reported side effect.
    for w in top_windows():
        if w.handle not in before and w.handle in touched:
            try:
                w.close()
            except Exception:
                pass
    time.sleep(1)
