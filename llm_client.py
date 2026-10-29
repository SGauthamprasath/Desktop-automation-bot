"""
llm_client.py
-------------
Wraps both backends behind one function, get_action(), so agent.py's
loop doesn't need to know or care which model is actually deciding
the action. This is also where the Phase 2 confidence gate will plug
in later - it wraps a call to get_action(), regardless of backend.
"""

import json
import re
import urllib.request

import config

# The example response below deliberately uses an element unlike the real
# tasks: an earlier example naming the "Edit" menu made the 7B model pick
# Edit regardless of the task (2/2 in a replay test; a neutral example fixed it).
SYSTEM_PROMPT = """You are a desktop automation agent. Each step you get the user's task, the
current window's numbered ELEMENTS (from Windows UI Automation), other text on
screen, the open windows, and the actions taken so far.

Decide the SINGLE next action. Respond with ONLY this JSON object:
{"reasoning": "<one short sentence>", "action_type": "<action>", "element": <number or null>, "target_name": "<that element's name>", "value": "<text>", "plan_step": <number or null>}

Write "reasoning" FIRST: what the screen shows and what moves the task forward.

ELEMENT ACTIONS - give the element's number in "element" and its name in
"target_name". Each element lists the actions it supports after "can:".
  invoke       click a button, menu item or link
  toggle       flip a checkbox, switch or checkable menu item
  select       select a list item, tab, file or radio button (without opening it)
  expand       open a menu, dropdown or tree node
  collapse     close a menu, dropdown or tree node
  set_value    put "value" into a text field (replaces its content) - use this
               whenever the text belongs in a listed field
  scroll       scroll that area; "value" = up, down, left or right
TOOL ACTIONS - "element": null; the argument goes in "value":
  open_app      launch an app by name; it becomes the current window
  switch_window make another open window current; "value" = part of its title
  press_keys    keyboard shortcut, e.g. "ctrl+s" or "enter"
  type_text     type "value" at the keyboard cursor - only when no listed text
                field fits (e.g. keyboard input to an app with no text box)
  wait          wait "value" seconds (at most 10)
done - the task is complete.

Rules:
- No current window, or the wrong app? Use open_app or switch_window first.
- Only choose an action listed in that element's "can:".
- An element marked "expanded" is already open: act on its items, never invoke
  it again (that closes it). Don't toggle or select something already in the
  state the task asks for.
- Read "ACTIONS SO FAR" and the screen changes: if the task is already
  complete, respond with "done". Never repeat an action that already succeeded.
- If a PLAN is given, work on the step marked "in progress" until it is
  finished, then the next one; put the number of the step this action is for
  in "plan_step". Stop with "done" as soon as DONE WHEN is true. Without a
  plan, "plan_step" is null.

Example, for a screen with [7] Button "Apply" | can: invoke:
{"reasoning": "The settings are filled in, so Apply saves them", "action_type": "invoke", "element": 7, "target_name": "Apply", "value": ""}
"""

PLANNER_PROMPT = """You plan desktop automation tasks on Windows. Given a task and what is on
screen, write a short plan: the steps needed to do EXACTLY what the task asks -
nothing more - and how to tell that the task is finished.

Respond with ONLY this JSON object:
{"steps": ["<step 1>", "<step 2>"], "done_when": "<what the screen shows when the task is complete>"}

Rules:
- 1 to 8 steps. Each step is a sub-goal like "Open the View menu" or
  "Calculate 15 + 27" - not single keystrokes or digits.
- Cover EVERY part of the task, and make "done_when" check every part.
- Do not add steps the task does not ask for. If the task is to open a menu,
  the plan ends once the menu is open - do not click anything inside it.
- If the needed app is not the current window, the first step opens or
  switches to it.

Example, task "Turn on dark mode in the app's settings":
{"steps": ["Open the Settings page", "Turn on the Dark mode switch"], "done_when": "The Dark mode switch is on"}
"""

MAX_PLAN_STEPS = 8  # 5 was too few: a 6-step plan got rejected and the retry silently dropped a step
PLAN_CONTEXT_ELEMENTS = 30  # element names shown to the planner - enough to be concrete

REQUIRED_KEYS = {"reasoning", "action_type", "element", "target_name", "value"}

# How each UIA pattern is shown to the model: as the action(s) it enables.
ACTIONS_FOR_PATTERN = {
    "invoke": ["invoke"], "toggle": ["toggle"], "selection_item": ["select"],
    "expand_collapse": ["expand", "collapse"], "value": ["set_value"], "scroll": ["scroll"],
}

ELEMENT_ACTIONS = {"invoke", "toggle", "select", "expand", "collapse", "set_value", "scroll", "click_fallback"}
TOOL_ACTIONS = {"open_app", "switch_window", "press_keys", "type_text", "wait"}  # mirrors tools.TOOL_ACTIONS
VALID_ACTION_TYPES = ELEMENT_ACTIONS | TOOL_ACTIONS | {"done"}

# The pattern each action type needs - used to break ties when grounding
# a target_name that several listed elements share.
PATTERN_FOR_ACTION = {
    "invoke": "invoke", "toggle": "toggle", "select": "selection_item",
    "expand": "expand_collapse", "collapse": "expand_collapse",
    "set_value": "value", "scroll": "scroll",
}


class ActionSchemaError(ValueError):
    """Raised when the model returns syntactically valid JSON that doesn't
    match the required action schema - e.g. a list of actions, or a dict
    with the wrong keys. This is the failure mode a plain JSONDecodeError
    check misses, since json.loads() succeeds on these."""


def _element_number(value):
    """The model's "element" as an int: accepts 7, "7", "[7]"; None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _validate_action(parsed, shown_elements: list) -> dict:
    """Checks the schema and resolves the model's element number to the
    element's internal UIA path, which is what the executor uses."""
    if not isinstance(parsed, dict):
        raise ActionSchemaError(f"expected a JSON object, got {type(parsed).__name__}")
    if "action_type" not in parsed:
        raise ActionSchemaError(f"missing required key 'action_type'. Got keys: {list(parsed.keys())}")
    action_type = parsed["action_type"]
    if action_type not in VALID_ACTION_TYPES:
        raise ActionSchemaError(f"action_type '{action_type}' is not one of {sorted(VALID_ACTION_TYPES)}")
    if action_type in ELEMENT_ACTIONS:
        number = _element_number(parsed.get("element"))
        by_id = {e["id"]: e for e in shown_elements}
        if number in by_id:
            parsed["path"] = by_id[number]["path"]
        elif str(parsed.get("target_name") or "").strip():
            parsed["path"] = []  # unknown number, but a name - grounding finds it by name
        elif number is None:
            raise ActionSchemaError(f"'{action_type}' acts on an element: give its number in 'element'")
        else:
            raise ActionSchemaError(f"there is no element [{number}] - use a number from the ELEMENTS list")
    if action_type in TOOL_ACTIONS - {"wait"} and not str(parsed.get("value") or "").strip():
        # Seen in testing: {"action_type": "open_app", "target_name": "Calculator", "value": ""}.
        # The intent is unambiguous, so accept target_name as the argument.
        if str(parsed.get("target_name") or "").strip():
            parsed["value"] = parsed["target_name"]
        else:
            raise ActionSchemaError(f"'{action_type}' needs its argument in 'value'")
    return parsed


# Actions whose effect is the same when repeated are the ones worth
# re-checking; repeating scroll is normal ("scroll down further"). wait is
# NOT exempt: the model once answered "no action is needed" and then chose
# wait 14 times in a row instead of done.
REPEATABLE_ACTIONS = {"scroll"}


def _check_action(action: dict, shown_elements: list, history: list, window_title=None) -> dict:
    """
    Deterministic sanity checks on an already-grounded action, each one
    added after a failure mode seen in testing. A rejection goes back to the
    model through get_action's retry loop with the reason spelled out.
    """
    action_type = action["action_type"]

    if action_type == "open_app" and window_title:
        # Seen in testing: open_app "Notepad" while already in Notepad opened
        # a second Notepad window, and the task was done in the wrong one.
        app_words = _words(str(action.get("value") or ""))
        if app_words and app_words <= _words(window_title):
            raise ActionSchemaError(
                f"the current window {window_title!r} already is that app - act in it instead of opening it again")

    if history:
        last = history[-1]
        from executor import NOTHING_MOVED
        same = all(str(last["action"].get(k)) == str(action.get(k))
                   for k in ("action_type", "path", "value"))
        had_no_effect = str(last.get("note", "")).startswith(NOTHING_MOVED)
        if same and last["result"] and (action_type not in REPEATABLE_ACTIONS or had_no_effect):
            # e.g. pressing Enter five times after a calculation, or typing
            # the same text twice - the model not noticing it already succeeded.
            raise ActionSchemaError(
                "you already did exactly this action in the previous step and it succeeded. "
                "If the task is now complete, respond with action_type \"done\"; otherwise choose a different action")

    if action_type not in ELEMENT_ACTIONS:
        return action
    element = next((e for e in shown_elements if e["path"] == action["path"]), None)
    if element is None:
        return action

    needed = PATTERN_FOR_ACTION.get(action_type)
    if needed and action_type != "scroll" and needed not in element["patterns"]:
        # e.g. "select" on the list container instead of on the item in it.
        raise ActionSchemaError(
            f"{element['name']!r} does not support '{action_type}' (it can: {', '.join(_actions_for(element))}). "
            f"Pick the element that does - for select, the item itself rather than the list around it")

    if action_type in ("invoke", "expand") and element.get("expanded") is True:
        raise ActionSchemaError(
            f"{element['name']!r} is already expanded (open) - invoking it again would close it. "
            "Act on one of its items, or respond \"done\" if opening it was the task")
    if action_type == "collapse" and element.get("expanded") is False:
        raise ActionSchemaError(f"{element['name']!r} is already collapsed")
    if action_type == "select" and element.get("selected") is True:
        raise ActionSchemaError(
            f"{element['name']!r} is already selected. Respond \"done\" if selecting it was the task")
    return action


def _ground_action(action: dict, shown_elements: list) -> dict:
    """
    Cross-checks the model's path against the element name it says it
    means. Small local models reliably pick the right element by NAME but
    often write the wrong index path - seen repeatedly in testing (e.g.
    "Selects the View menu" with the path of the Bold button). Names are
    what the model actually reasons about, so they're the ground truth here:

    - path and target_name agree               -> unchanged
    - they disagree, and exactly one shown
      element has that name (ties broken by
      the pattern the action needs)            -> path corrected, logged
    - path isn't a shown element, or the name
      is unknown/ambiguous                     -> ActionSchemaError, so the
                                                  retry loop feeds it back
    """
    if action["action_type"] not in ELEMENT_ACTIONS:
        return action  # tools and done don't target an element
    return _match_click_pattern(_ground_path(action, shown_elements), shown_elements)


# invoke, toggle, select and expand all amount to "click it". The model often
# picks one the element doesn't expose - e.g. invoke on a checkable menu item
# that only has Toggle, or on a menu that only has ExpandCollapse - which the
# executor would then reject. Swap to the first one the element does support.
CLICK_LIKE_ORDER = ["invoke", "toggle", "select", "expand"]


def _match_click_pattern(action: dict, shown_elements: list) -> dict:
    action_type = action["action_type"]
    element = next((e for e in shown_elements if e["path"] == action["path"]), None)
    if action_type not in CLICK_LIKE_ORDER or element is None:
        return action
    if PATTERN_FOR_ACTION[action_type] in element["patterns"]:
        return action
    for other in CLICK_LIKE_ORDER:
        if PATTERN_FOR_ACTION[other] in element["patterns"]:
            print(f"  [info] {element['name']!r} has no '{action_type}' pattern - using '{other}'")
            return {**action, "action_type": other}
    return action


def _ground_path(action: dict, shown_elements: list) -> dict:
    by_path = {tuple(e["path"]): e for e in shown_elements}
    chosen = by_path.get(tuple(action["path"]))
    target = str(action.get("target_name") or "").strip().lower()

    if not target:
        if chosen is None:
            raise ActionSchemaError("that element is not in the ELEMENTS list")
        return action
    if chosen is not None and chosen["name"].strip().lower() == target:
        return action

    matches = [e for e in shown_elements if e["name"].strip().lower() == target]
    needed = PATTERN_FOR_ACTION.get(action["action_type"])
    if len(matches) > 1 and needed:
        matches = [e for e in matches if needed in e["patterns"]] or matches
    if len(matches) == 1:
        found = f"[{chosen['id']}] {chosen['name']!r}" if chosen else "not a listed element"
        print(f"  [info] element {action.get('element')!r} is {found}, not {action['target_name']!r} - "
              f"using [{matches[0]['id']}] instead")
        return {**action, "path": matches[0]["path"]}

    if not matches:
        # Seen in testing: toggling a menu item closes the menu, the model then
        # asks for the next item in it three times and gives up.
        raise ActionSchemaError(
            f"no element in the ELEMENTS list is named {action['target_name']!r} - it is not on screen right now. "
            "Items inside a menu or dropdown only appear while it is open: if it closed, expand it again first")
    raise ActionSchemaError(f"several elements are named {action['target_name']!r} - "
                            f"give the number of the one you mean")


def _task_relevance(element: dict, task_words: set) -> int:
    """Crude relevance score: how many task words appear in this
    element's name. Elements matching the task get sorted ahead of
    generic toolbar clutter instead of being lost past the cap - this
    is what let the model miss the Word Wrap checkbox and a named
    folder in testing, both present but outranked by earlier, more
    generic elements."""
    return len(task_words & _words(element["name"]))


def _words(text: str) -> set:
    # Punctuation-insensitive, so "'Hello" in a task still matches "Hello".
    return set(re.findall(r"[a-z0-9]+", text.lower()))


# Patterns executor.py can actually act on.
EXECUTABLE_PATTERNS = set(PATTERN_FOR_ACTION.values())

# Only these fields are kept per element. bounding_box, auto_id and visible
# roughly doubled the prompt size in testing without being used. "path" is
# kept for mapping the model's element number back to the UI - it is never
# shown to the model.
KEPT_FIELDS = ("path", "control_type", "name", "patterns")

WINDOW_CHROME_TYPES = {"Window", "TitleBar"}

MAX_ELEMENTS = 50     # numbered, actionable elements
MAX_TEXTS = 25        # unnumbered "other text on screen"
MAX_NAME_CHARS = 80


def _slim(element: dict) -> dict:
    slim = {k: element[k] for k in KEPT_FIELDS}
    for key in ("toggled", "expanded", "selected"):  # see perception._pattern_state
        if key in element:
            slim[key] = element[key]
    if not element.get("enabled", True):
        slim["enabled"] = False
    return slim


def _actions_for(element: dict) -> list:
    actions = []
    for pattern in element["patterns"]:
        actions += [a for a in ACTIONS_FOR_PATTERN.get(pattern, []) if a not in actions]
    return actions


def _clean(text: str) -> str:
    text = " ".join(str(text).split())  # newlines/tabs -> single spaces
    return text if len(text) <= MAX_NAME_CHARS else text[:MAX_NAME_CHARS - 1] + "…"


def _compact_elements(screen_state: dict, task: str = "") -> dict:
    """
    Trims the element list sent to the LLM (perception.py's own saved
    snapshot is untouched - this only affects what goes in the prompt).

    Two problems this addresses, both seen in testing:
    1. Noise: unrelated elements (system tray icons, clock, battery) can
       dominate the tree and have nothing to do with the task.
    2. Size: a large tree (e.g. after opening a menu with a dozen items)
       is what correlated with the model drifting off the required JSON
       schema - fewer, more relevant elements means a shorter, more
       focused prompt.

    Actionable elements (a pattern the executor can use) are numbered
    1..N - the model refers to them by that number, which small models get
    right far more often than copying nested UIA paths like [1, 1, 1, 6, 4]
    (a constant source of wrong-element errors in testing). Named elements
    with nothing to act on (labels, status text such as "Display is 19")
    are kept as unnumbered "texts", so the model can still read them.
    """
    # Window-chrome controls (Close/Minimize/etc.) should never be offered
    # as options at all - a real near-miss in testing had the model
    # repeatedly clicking these out of confusion. Filtering them out here
    # is the primary defense; executor.py's is_dangerous check is the
    # backup in case one slips through some other path.
    from executor import is_dangerous

    elements = [e for e in screen_state["elements"] if not is_dangerous(e["name"])]

    # The window/title bar objects themselves expose a Value pattern (their
    # title), which showed up as `[1] Window "Calculator" | can: set_value` -
    # never a real target, and an invitation to overwrite the window title.
    elements = [e for e in elements if e["control_type"] not in WINDOW_CHROME_TYPES]

    def executable(e):
        return bool(EXECUTABLE_PATTERNS & set(e["patterns"]))

    # Only what is actually on screen. Off-screen elements were about a
    # quarter of the prompt in testing - Notepad's hidden tabs (names of the
    # user's open files), Explorer's navigation tree (OneDrive folders), and
    # items of menus that had already closed, which the model kept trying to
    # click. Noise for the model and needless exposure of personal data
    # (suggestion/security.md, Layer 1). Scrolled-away content can be
    # brought back with the scroll action.
    elements = [e for e in elements if e.get("visible", True)]

    # A child label that just repeats its parent's name (e.g. the Text
    # inside a TabItem or ListItem) adds tokens but no information.
    names_by_path = {tuple(e["path"]): e["name"].lower() for e in elements}

    def redundant_label(e):
        parent_name = names_by_path.get(tuple(e["path"][:-1]), "")
        return bool(parent_name) and e["name"].strip().lower() in parent_name

    task_words = _words(task)
    actionable = [e for e in elements if executable(e)]
    named_only = [e for e in elements
                  if not executable(e) and e["name"].strip() and not redundant_label(e)]
    actionable.sort(key=lambda e: _task_relevance(e, task_words), reverse=True)

    elements = [{**_slim(e), "id": i} for i, e in enumerate(actionable[:MAX_ELEMENTS], start=1)]
    texts = []
    for e in named_only:
        text = _clean(e["name"])
        if text not in texts:
            texts.append(text)
    return {
        "window_title": screen_state["window_title"],  # None = no current window yet
        "elements": elements,
        "texts": texts[:MAX_TEXTS],
        "open_windows": screen_state.get("open_windows", []),
    }


def _render_element(e: dict) -> str:
    line = f'[{e["id"]}] {e["control_type"]} "{_clean(e["name"])}" | can: {", ".join(_actions_for(e))}'
    state = []
    if "toggled" in e:
        state.append("on" if e["toggled"] else "off")
    if "expanded" in e:
        state.append("expanded" if e["expanded"] else "collapsed")
    if e.get("selected"):
        state.append("selected")
    if e.get("enabled") is False:
        state.append("disabled")
    return line + (f" | {', '.join(state)}" if state else "")


def _render_history(history: list) -> str:
    """Past actions as short lines that refer to elements by NAME - element
    numbers are re-assigned every step, so old numbers would mislead."""
    if not history:
        return "(none yet)"
    lines = []
    for i, h in enumerate(history, start=1):
        a = h["action"]
        action_type, value = a["action_type"], str(a.get("value") or "")
        if action_type in ELEMENT_ACTIONS:
            line = f'{i}. {action_type} "{_clean(a.get("target_name") or "")}"'
            if value and action_type in ("set_value", "scroll"):
                line += f' value="{_clean(value)}"'
        else:
            line = f'{i}. {action_type} "{_clean(value)}"'
        if isinstance(a.get("plan_step"), int):
            line += f" [plan step {a['plan_step']}]"
        line += " -> OK" if h["result"] else " -> FAILED"
        if h.get("note"):
            line += f" ({h['note']})"
        changes = h.get("screen_changes") or {}
        described = []
        if "window" in changes:
            described.append(f"window is now {changes['window']!r}")
        if changes.get("appeared"):
            described.append("appeared: " + ", ".join(f'"{_clean(t)}"' for t in changes["appeared"]))
        if changes.get("gone"):
            described.append("gone: " + ", ".join(f'"{_clean(t)}"' for t in changes["gone"]))
        if changes.get("note"):
            described.append(changes["note"])
        if described:
            line += " | screen: " + "; ".join(described)
        lines.append(line)
    return "\n".join(lines)


def _plan_progress(history: list) -> int:
    """Highest plan step a successful action has worked on (0 = not started).
    Steps before it count as done - in testing, the model without this lost
    its place, e.g. switching back to Notepad before calculating anything."""
    steps = [h["action"].get("plan_step") for h in history if h["result"]]
    return max([s for s in steps if isinstance(s, int)], default=0)


def _render_plan(plan, history: list) -> list:
    if not plan:
        return []
    current = max(_plan_progress(history), 1)
    lines = ["PLAN (a guide - follow it unless the screen shows it is wrong):"]
    for i, step in enumerate(plan["steps"], start=1):
        marker = " (done)" if i < current else " (in progress)" if i == current else ""
        lines.append(f"{i}. {step}{marker}")
    lines.append(f"DONE WHEN: {plan['done_when']}")
    return lines + [""]


def _build_user_message(task, compact_screen, history, plan=None):
    # Plain text rather than JSON: about half the size for the same content,
    # and the model no longer sees element-shaped JSON objects it can mistake
    # for the shape of its own answer.
    title = compact_screen["window_title"]
    parts = [
        f"TASK: {task}",
        "",
        *_render_plan(plan, history),
        f"CURRENT WINDOW: {title!r}" if title else "CURRENT WINDOW: (none - use open_app or switch_window)",
        "OPEN WINDOWS: " + (" | ".join(_clean(t) for t in compact_screen["open_windows"]) or "(none)"),
        "ELEMENTS:",
        "\n".join(_render_element(e) for e in compact_screen["elements"]) or "(none)",
        "OTHER TEXT ON SCREEN: " + (" | ".join(f'"{t}"' for t in compact_screen["texts"]) or "(none)"),
        "ACTIONS SO FAR:",
        _render_history(history),
        "",
        # Repeated AFTER the data: smaller models weight the end of the
        # context most heavily.
        ("First check ACTIONS SO FAR against DONE WHEN: if the task is already complete, respond with \"done\". "
         if plan else
         "First check ACTIONS SO FAR: if the task is already complete, respond with \"done\". ")
        + "Otherwise respond with ONE JSON object: reasoning, action_type, element, target_name, value.",
    ]
    return "\n".join(parts)


def _validate_plan(parsed) -> dict:
    if not isinstance(parsed, dict):
        raise ActionSchemaError(f"expected a JSON object, got {type(parsed).__name__}")
    steps = parsed.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) and s.strip() for s in steps):
        raise ActionSchemaError("'steps' must be a non-empty list of short instructions")
    if len(steps) > MAX_PLAN_STEPS:
        raise ActionSchemaError(f"use at most {MAX_PLAN_STEPS} steps")
    done_when = str(parsed.get("done_when") or "").strip()
    if not done_when:
        raise ActionSchemaError("'done_when' must say what the screen shows when the task is complete")
    return {"steps": [_clean(s) for s in steps], "done_when": _clean(done_when)}


def make_plan(task: str, screen_state: dict):
    """
    One model call before acting: a short plan plus a "done when" condition,
    which the actor then sees at every step. Aimed at the failures that
    one-step-at-a-time decisions kept producing in testing - typing numbers
    in the wrong order, an extra Enter after "=", clicking an item after the
    menu the task asked for was already open, scrolling on for 10 steps.

    Returns {"steps": [...], "done_when": "..."}, or None if the model can't
    produce a valid plan - the agent then simply runs without one.
    """
    compact = _compact_elements(screen_state, task=task)
    title = compact["window_title"]
    names = [f'{e["control_type"]} "{_clean(e["name"])}"' for e in compact["elements"][:PLAN_CONTEXT_ELEMENTS] if e["name"].strip()]
    user_message = "\n".join([
        f"TASK: {task}",
        f"CURRENT WINDOW: {title!r}" if title else "CURRENT WINDOW: (none)",
        "OPEN WINDOWS: " + (" | ".join(_clean(t) for t in compact["open_windows"]) or "(none)"),
        "SOME ELEMENTS ON SCREEN: " + ("; ".join(names) or "(none)"),
    ])
    messages = [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": user_message}]
    call = _call_local if config.BACKEND == "local" else _call_cloud
    for attempt in range(config.MAX_RETRIES_ON_BAD_JSON + 1):
        raw = call(messages, 0 if attempt == 0 else 0.4)
        try:
            return _validate_plan(json.loads(_strip_fences(raw)))
        except (json.JSONDecodeError, ActionSchemaError) as e:
            print(f"  [warn] planner returned an invalid plan (attempt {attempt + 1}): {e}")
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That plan was invalid: {e}. Try again with the exact JSON shape."}]
    return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_local(messages: list, temperature: float) -> str:
    # Uses Ollama's native /api/chat rather than its OpenAI-compatible
    # endpoint, because only the native API accepts num_ctx. Without it
    # Ollama falls back to a ~2048-token window and silently truncates the
    # front of the prompt - confirmed in testing: the model never saw the
    # system prompt or the task, and acted on the tail of the element list.
    body = {
        "model": config.LOCAL_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",  # forces syntactically valid JSON
        "options": {"temperature": temperature, "num_ctx": config.LOCAL_NUM_CTX},
    }
    request = urllib.request.Request(
        config.OLLAMA_BASE_URL.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)

    # Truncation is silent on Ollama's side, so surface it here.
    prompt_tokens = result.get("prompt_eval_count") or 0
    if prompt_tokens >= config.LOCAL_NUM_CTX - 16:
        print(f"  [warn] prompt filled the context window ({prompt_tokens}/{config.LOCAL_NUM_CTX} "
              f"tokens) and was likely truncated - raise LOCAL_NUM_CTX")
    return result["message"]["content"]


def _call_cloud(messages: list, temperature: float) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Anthropic's system prompt is a separate top-level field, not a
    # role:"system" message - split it back out of the messages list.
    system = next(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] != "system"]

    response = client.messages.create(
        model=config.CLOUD_MODEL,
        max_tokens=500,
        system=system,
        temperature=temperature,
        messages=convo,
    )
    return response.content[0].text


def get_action(task: str, screen_state: dict, history: list, plan=None) -> dict:
    """
    Returns a parsed action dict.

    On failure, this does NOT just resend the identical prompt - at
    temperature 0 that's deterministic and would fail identically every
    time (this is exactly what happened before this fix: 3/3 identical
    wrong responses). Instead, each retry:
      1. appends the model's own bad response plus an explicit
         correction message explaining what was wrong, so the model
         can actually self-correct instead of blindly repeating itself
      2. raises the temperature slightly, so even if the correction
         message doesn't fully land, the retry isn't guaranteed to
         reproduce the exact same failure
    """
    compact_screen = _compact_elements(screen_state, task=task)
    user_message = _build_user_message(task, compact_screen, history, plan)
    call = _call_local if config.BACKEND == "local" else _call_cloud

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    last_error = None
    temperatures = [0, 0.4, 0.7]
    for attempt in range(config.MAX_RETRIES_ON_BAD_JSON + 1):
        temp = temperatures[min(attempt, len(temperatures) - 1)]
        raw = call(messages, temp)
        cleaned = _strip_fences(raw)
        try:
            parsed = json.loads(cleaned)
            shown = compact_screen["elements"]
            action = _validate_action(parsed, shown)  # catches valid-JSON-wrong-shape too
            action = _ground_action(action, shown)
            action = _check_action(action, shown, history, compact_screen["window_title"])
            step = _element_number(action.get("plan_step"))
            action["plan_step"] = step if plan and step and 1 <= step <= len(plan["steps"]) else None
            # Record the element's real name, so the history (which refers to
            # elements by name) is accurate even if the model gave only a number.
            target = next((e for e in shown if e["path"] == action.get("path")), None)
            if target is not None:
                action["target_name"] = target["name"]
            return action
        except (json.JSONDecodeError, ActionSchemaError) as e:
            last_error = e
            kind = "invalid JSON" if isinstance(e, json.JSONDecodeError) else "wrong schema"
            print(f"  [warn] backend='{config.BACKEND}' returned {kind} "
                  f"(attempt {attempt + 1}/{config.MAX_RETRIES_ON_BAD_JSON + 1}, temp={temp}): {e}")
            print(f"         raw response: {raw[:300]!r}")

            # Build the correction turn for the next attempt.
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": (
                f"That response was invalid: {e}. "
                "Respond with exactly one JSON object with keys "
                "reasoning, action_type, element, target_name, value - describing ONE "
                "action. Try again."
            )})

    raise ValueError(f"LLM returned invalid/malformed action after retries: {last_error}")