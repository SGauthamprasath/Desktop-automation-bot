"""
tools.py
--------
Tool layer - actions that don't target a single UI element.

Many basic tasks don't need the UI tree at all: "open Calculator" is one
launch call, not a walk through the Start menu; Ctrl+S is one shortcut.
These tools give the model those capabilities directly:

    open_app       launch an installed app by name; it becomes the current window
    switch_window  make another open window the current one
    press_keys     keyboard shortcut(s) in the current window ("ctrl+s", "enter")
    type_text      type text at the keyboard cursor of the current window
    wait           pause while something loads

Every tool returns (ExecutionResult, window): `window` is the new current
window when the tool changed it (open_app, switch_window), otherwise None.

press_keys and type_text are simulated input: they need the target window in
the foreground, so they DO take focus - logged as "input_simulation" so Phase
2's cost model can count them.

Safety rules here are deliberately deterministic (see suggestion/security.md):
shells can't be launched, keyboard input is never sent to a terminal window,
input is refused if the target didn't actually come to the front, and a few
key combinations that close windows or open a command runner are blocked.
"""

import json
import re
import subprocess
import time

from pywinauto import Desktop
from pywinauto.keyboard import send_keys

from executor import ExecutionResult

TOOL_ACTIONS = {"open_app", "switch_window", "press_keys", "type_text", "wait"}

APP_LAUNCH_TIMEOUT_SECONDS = 15
# Single-instance apps may reuse an existing window instead of opening a new
# one; after this long with no new window, accept an existing matching one.
NEW_WINDOW_GRACE_SECONDS = 3
APP_SETTLE_SECONDS = 1.0  # let a fresh window populate its UI tree
MAX_WAIT_SECONDS = 10

# Apps that would let a confused or hijacked model run arbitrary commands -
# including "Windows Tools", a launcher for cmd, PowerShell and regedit.
BLOCKED_APP_WORDS = ("command prompt", "powershell", "terminal", "registry editor", "regedit", "cmd",
                     "windows tools")


def _is_blocked(name: str) -> bool:
    return any(word in name.lower() for word in BLOCKED_APP_WORDS)

# Window classes of console/terminal hosts. Keyboard input to these could
# execute commands - including in the terminal running this agent.
TERMINAL_WINDOW_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "PseudoConsoleWindow"}

# Normalized combos (modifiers in a fixed order) that close things or open a
# command runner / lock screen.
# ctrl+q / ctrl+shift+w quit or close the whole window in many apps.
BLOCKED_KEY_COMBOS = {"alt+f4", "ctrl+w", "ctrl+f4", "ctrl+q", "ctrl+shift+w", "win+r", "win+x", "win+l"}

# Shell windows that are always present and never a meaningful target.
IGNORED_WINDOW_TITLES = {"Taskbar", "Program Manager"}

_app_index = None


def _start_apps() -> list:
    """All Start-menu apps (classic and Store) as [{"Name", "AppID"}],
    from PowerShell's Get-StartApps. Takes a few seconds, so it's cached
    for the life of the process."""
    global _app_index
    if _app_index is None:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        ).stdout
        parsed = json.loads(out) if out.strip() else []
        _app_index = parsed if isinstance(parsed, list) else [parsed]
    return _app_index


def _find_app(name: str):
    """Best Start-menu match for `name`: exact, then prefix, then all words
    contained. Returns the app dict or None.

    No "close enough" fuzzy matching: it once turned "Windows Terminal" into
    "Windows Tools" and launched that instead. Better to report no match
    and let the model name the app properly."""
    wanted = name.strip().lower()
    apps = _start_apps()
    names = {a["Name"].lower(): a for a in apps}
    if wanted in names:
        return names[wanted]
    prefixed = [a for a in apps if a["Name"].lower().startswith(wanted)]
    if prefixed:
        return min(prefixed, key=lambda a: len(a["Name"]))
    words = set(re.findall(r"\w+", wanted))
    containing = [a for a in apps if words and words <= set(re.findall(r"\w+", a["Name"].lower()))]
    if containing:
        return min(containing, key=lambda a: len(a["Name"]))
    return None


def _top_windows() -> list:
    return [w for w in Desktop(backend="uia").windows()
            if w.window_text() and w.window_text() not in IGNORED_WINDOW_TITLES]


def list_open_windows(limit: int = 15) -> list:
    """Titles of open top-level windows, front to back - shown to the model
    so it can pick a switch_window target. Titles only, never contents."""
    try:
        return [w.window_text() for w in _top_windows()][:limit]
    except Exception:
        return []


def _spec(handle):
    # Same object type connect_to_app() returns, so the rest of the
    # pipeline treats launched/switched windows exactly like connected ones.
    return Desktop(backend="uia").window(handle=handle)


def _is_terminal(window) -> bool:
    try:
        return window.element_info.class_name in TERMINAL_WINDOW_CLASSES
    except Exception:
        return False


def _bring_to_front(window) -> bool:
    """Focuses the window and confirms it really is the foreground window -
    otherwise simulated keystrokes would land in whatever app IS in front."""
    import ctypes
    try:
        window.set_focus()
        time.sleep(0.3)
        return ctypes.windll.user32.GetForegroundWindow() == window.element_info.handle
    except Exception:
        return False


def open_app(name: str):
    # Check what was ASKED for, not only what it matched: a blocked request
    # must never resolve to some other, unblocked app.
    if _is_blocked(name):
        return ExecutionResult(False, "blocked", error=f"refused: launching {name!r} is not allowed"), None
    app = _find_app(name)
    if app is None:
        return ExecutionResult(False, "tool", error=f"no installed app matches {name!r}"), None
    if _is_blocked(app["Name"]):
        return ExecutionResult(False, "blocked", error=f"refused: launching {app['Name']!r} is not allowed"), None

    before = {w.handle for w in _top_windows()}
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"])

    app_words = set(re.findall(r"\w+", app["Name"].lower()))
    started = time.time()
    while time.time() - started < APP_LAUNCH_TIMEOUT_SECONDS:
        time.sleep(0.3)
        windows = _top_windows()
        new = [w for w in windows if w.handle not in before]
        if not new and time.time() - started > NEW_WINDOW_GRACE_SECONDS:
            new = [w for w in windows if app_words <= set(re.findall(r"\w+", w.window_text().lower()))]
        if new:
            time.sleep(APP_SETTLE_SECONDS)
            window = _spec(new[0].handle)
            title = window.window_text()
            return ExecutionResult(True, "tool", detail=f"opened {app['Name']!r}; current window is now {title!r}"), window

    return ExecutionResult(False, "tool", error=f"launched {app['Name']!r} but no window appeared "
                                                f"within {APP_LAUNCH_TIMEOUT_SECONDS}s"), None


def switch_window(title: str):
    wanted = title.strip().lower()
    windows = _top_windows()
    matches = [w for w in windows if wanted in w.window_text().lower()]
    if not matches:
        wanted_words = set(re.findall(r"\w+", wanted))
        scored = [(len(wanted_words & set(re.findall(r"\w+", w.window_text().lower()))), w) for w in windows]
        best = max(scored, key=lambda s: s[0], default=(0, None))
        matches = [best[1]] if best[0] > 0 else []
    if not matches:
        return ExecutionResult(False, "tool", error=f"no open window matches {title!r}; "
                                                    f"open windows: {list_open_windows()}"), None

    window = _spec(matches[0].handle)  # windows are front-to-back, so the first match is the most recent
    if _is_terminal(window):
        return ExecutionResult(False, "blocked", error="refused: switching to a terminal window is not allowed"), None
    focused = _bring_to_front(window)
    note = "" if focused else " (could not bring it to the front)"
    return ExecutionResult(True, "tool", detail=f"current window is now {window.window_text()!r}{note}"), window


_MODIFIERS = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}
_MODIFIER_ORDER = ["ctrl", "shift", "alt", "win"]
_NAMED_KEYS = {
    "enter": "ENTER", "return": "ENTER", "tab": "TAB", "esc": "ESC", "escape": "ESC",
    "space": "SPACE", "backspace": "BACKSPACE", "delete": "DELETE", "del": "DELETE",
    "insert": "INSERT", "home": "HOME", "end": "END", "pageup": "PGUP", "pgup": "PGUP",
    "pagedown": "PGDN", "pgdn": "PGDN", "up": "UP", "down": "DOWN", "left": "LEFT",
    "right": "RIGHT", "plus": "+", "minus": "-",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
_SPECIAL_CHARS = set("+^%~(){}[]")  # must be wrapped in {} for send_keys


def _key_token(key: str) -> str:
    if key in _NAMED_KEYS:
        named = _NAMED_KEYS[key]
        return "{" + named + "}"
    if len(key) == 1:
        return "{" + key + "}" if key in _SPECIAL_CHARS else key
    raise ValueError(f"unknown key {key!r}")


def _parse_combo(combo: str):
    """'Ctrl+Shift+S' -> ('ctrl+shift+s', '^+s'). Raises ValueError."""
    parts = [p for p in combo.lower().replace("control", "ctrl").split("+") if p]
    if combo.endswith("++"):  # "ctrl++" means ctrl and the plus key
        parts.append("+")
    *mods, key = parts
    unknown = [m for m in mods if m not in _MODIFIERS and m != "win"]
    if unknown or not key:
        raise ValueError(f"can't parse key combination {combo!r}")
    mods = sorted(set(mods), key=_MODIFIER_ORDER.index)
    normalized = "+".join(mods + [key])
    keys = "".join(_MODIFIERS[m] for m in mods if m != "win") + _key_token(key)
    if "win" in mods:
        keys = "{VK_LWIN down}" + keys + "{VK_LWIN up}"
    return normalized, keys


def press_keys(window, keys: str):
    """keys: one or more combos separated by spaces or commas,
    e.g. "ctrl+s" or "ctrl+a, delete"."""
    if window is None:
        return ExecutionResult(False, "tool", error="no current window - use open_app or switch_window first"), None
    keys = re.sub(r"\s*\+\s*", "+", keys.strip())  # "Ctrl + S" -> "Ctrl+S"
    try:
        combos = [_parse_combo(c) for c in re.split(r"[,\s]+", keys) if c]
    except ValueError as e:
        return ExecutionResult(False, "tool", error=str(e)), None
    if not combos:
        return ExecutionResult(False, "tool", error="no keys given"), None
    blocked = [n for n, _ in combos if n in BLOCKED_KEY_COMBOS]
    if blocked:
        return ExecutionResult(False, "blocked", error=f"refused: {blocked[0]!r} is a blocked key combination"), None
    if _is_terminal(window):
        return ExecutionResult(False, "blocked", error="refused: keyboard input to a terminal window is not allowed"), None
    if not _bring_to_front(window):
        return ExecutionResult(False, "tool", error="could not bring the window to the front, so keys were not sent"), None

    send_keys("".join(k for _, k in combos), pause=0.05)
    return ExecutionResult(True, "input_simulation"), None


def type_text(window, text: str):
    if window is None:
        return ExecutionResult(False, "tool", error="no current window - use open_app or switch_window first"), None
    if _is_terminal(window):
        return ExecutionResult(False, "blocked", error="refused: keyboard input to a terminal window is not allowed"), None
    if not _bring_to_front(window):
        return ExecutionResult(False, "tool", error="could not bring the window to the front, so text was not typed"), None

    escaped = "".join(
        "{ENTER}" if ch == "\n" else "{TAB}" if ch == "\t" else "{" + ch + "}" if ch in _SPECIAL_CHARS else ch
        for ch in text
    )
    send_keys(escaped, with_spaces=True, pause=0.02)
    return ExecutionResult(True, "input_simulation"), None


def wait(seconds) -> tuple:
    try:
        seconds = float(seconds or 1)
    except (TypeError, ValueError):
        seconds = 1.0
    seconds = max(0.5, min(seconds, MAX_WAIT_SECONDS))
    time.sleep(seconds)
    return ExecutionResult(True, "tool", detail=f"waited {seconds:g}s"), None


def run_tool(action: dict, window):
    """Dispatches a tool action. Returns (ExecutionResult, new_window_or_None)."""
    action_type = action["action_type"]
    value = str(action.get("value") or "")
    try:
        if action_type == "open_app":
            return open_app(value)
        if action_type == "switch_window":
            return switch_window(value)
        if action_type == "press_keys":
            return press_keys(window, value)
        if action_type == "type_text":
            return type_text(window, value)
        if action_type == "wait":
            return wait(value)
    except Exception as e:
        return ExecutionResult(False, "tool", error=f"{type(e).__name__}: {str(e) or '(no message)'}"), None
    return ExecutionResult(False, "none", error=f"unknown tool: {action_type}"), None
