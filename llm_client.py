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
SYSTEM_PROMPT = """You are a desktop automation agent. You are given:
1. A task instruction from the user.
2. The current window's UI as a list of elements (from Windows UI Automation), each with a "path" you must use to reference it, plus the titles of other open windows.

Decide the SINGLE next action to take. Respond with ONLY a JSON object, no other text, in this exact shape:

{
  "reasoning": "<one short sentence: what the screen shows and what moves the task forward>",
  "action_type": "<one of the actions below>",
  "path": [<indices>],
  "target_name": "<the exact "name" of the element at that path>",
  "value": "<text>"
}

Write "reasoning" FIRST - decide what to do before you pick the action.

ELEMENT ACTIONS - act on one element of current_screen. "path" and
"target_name" must describe the SAME listed element:
  invoke         click a button, menu item or link
  toggle         flip a checkbox, switch or checkable menu item
  select         select a list item, tab, file or radio button (without opening it)
  expand         open a menu, dropdown or tree node
  collapse       close a menu, dropdown or tree node
  set_value      put "value" into a listed text field (replaces its content) -
                 use this whenever the text belongs in a field that is listed
  scroll         scroll the element's area; "value" = up, down, left or right
  click_fallback real mouse click - ONLY if no other action can do it

TOOL ACTIONS - no path needed; the argument goes in "value":
  open_app       launch an app by name, e.g. "Calculator"; it becomes the current window
  switch_window  make another open window current; "value" = part of its title from "open_windows"
  press_keys     keyboard shortcut in the current window, e.g. "ctrl+s" or "enter"
  type_text      type "value" at the keyboard cursor - only when no listed text
                 field fits (e.g. keyboard input to an app with no text box)
  wait           wait "value" seconds (at most 10) for something to load

done             the task is complete - no path or value needed

If there is no current window, or it is the wrong app, use open_app or
switch_window first. Prefer element actions; use press_keys / type_text when
no element does the job.

Elements may report their state: "toggled" (true = on), "expanded"
(true = open), "selected" (true = selected). Use these: don't re-invoke a
menu that is already expanded (that closes it) - act on its items instead;
don't toggle or select something that is already in the state the task asks for.

Check "actions_taken_so_far" before deciding: if you already successfully
performed the action the task needs, respond with action_type "done" instead
of repeating it.

Respond with ONLY that single JSON object - one object, the keys shown above,
nothing else. NEVER return a list of actions, NEVER wrap it in an "actions" key,
NEVER echo back the input. Decide ONE action only, even if several elements look
relevant - you will be asked again next step.

Example of a correct response, given a screen with a button named "Apply" at path [1, 2]:
{"reasoning": "The settings are filled in, so Apply saves them", "action_type": "invoke", "path": [1, 2], "target_name": "Apply", "value": ""}

Respond with ONLY the JSON object. No markdown fences, no commentary before or after it.
"""

REQUIRED_KEYS = {"action_type", "path", "target_name", "value", "reasoning"}

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


def _validate_action(parsed) -> dict:
    if not isinstance(parsed, dict):
        raise ActionSchemaError(f"expected a JSON object, got {type(parsed).__name__}")
    if "action_type" not in parsed:
        raise ActionSchemaError(f"missing required key 'action_type'. Got keys: {list(parsed.keys())}")
    action_type = parsed["action_type"]
    if action_type not in VALID_ACTION_TYPES:
        raise ActionSchemaError(f"action_type '{action_type}' is not one of {sorted(VALID_ACTION_TYPES)}")
    if action_type in ELEMENT_ACTIONS:
        # target_name alone is enough - grounding can find the path by name.
        if "path" not in parsed and not parsed.get("target_name"):
            raise ActionSchemaError(f"'{action_type}' acts on an element: give its 'path' and 'target_name'")
        parsed.setdefault("path", [])
    if action_type in TOOL_ACTIONS - {"wait"} and not str(parsed.get("value") or "").strip():
        # Seen in testing: {"action_type": "open_app", "target_name": "Calculator", "value": ""}.
        # The intent is unambiguous, so accept target_name as the argument.
        if str(parsed.get("target_name") or "").strip():
            parsed["value"] = parsed["target_name"]
        else:
            raise ActionSchemaError(f"'{action_type}' needs its argument in 'value'")
    return parsed


# Actions whose effect is the same when repeated are the ones worth
# re-checking; repeating scroll or wait is normal ("scroll down further").
REPEATABLE_ACTIONS = {"scroll", "wait"}


def _check_action(action: dict, shown_elements: list, history: list) -> dict:
    """
    Deterministic sanity checks on an already-grounded action, each one
    added after a failure mode seen in testing. A rejection goes back to the
    model through get_action's retry loop with the reason spelled out.
    """
    action_type = action["action_type"]

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
            f"{element['name']!r} does not support '{action_type}' (it supports {element['patterns']}). "
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
    path = action["path"]
    by_path = {tuple(e["path"]): e for e in shown_elements}
    at_path = by_path.get(tuple(path))
    target = str(action.get("target_name") or "").strip().lower()

    if not target:
        if at_path is None:
            raise ActionSchemaError(f"path {path} is not one of the elements in current_screen")
        return action
    if at_path is not None and at_path["name"].strip().lower() == target:
        return action

    matches = [e for e in shown_elements if e["name"].strip().lower() == target]
    needed = PATTERN_FOR_ACTION.get(action["action_type"])
    if len(matches) > 1 and needed:
        matches = [e for e in matches if needed in e["patterns"]] or matches
    if len(matches) == 1:
        found = at_path["name"] if at_path else "no listed element"
        print(f"  [info] path {path} is {found!r}, not {action['target_name']!r} - "
              f"using {matches[0]['path']} instead")
        return {**action, "path": matches[0]["path"]}

    if not matches:
        raise ActionSchemaError(f"no element in current_screen is named {action['target_name']!r}")
    raise ActionSchemaError(f"several elements are named {action['target_name']!r} - "
                            f"pick one by its exact path")


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

# Only these fields help the model choose an action. bounding_box, auto_id
# and visible roughly doubled the prompt size in testing without being used.
PROMPT_FIELDS = ("path", "control_type", "name", "patterns")


def _slim(element: dict) -> dict:
    slim = {k: element[k] for k in PROMPT_FIELDS}
    for key in ("toggled", "expanded", "selected"):  # see perception._pattern_state
        if key in element:
            slim[key] = element[key]
    if not element.get("enabled", True):
        slim["enabled"] = False  # only worth the tokens when it's unusual
    return slim


def _compact_elements(screen_state: dict, task: str = "", max_elements: int = 60) -> dict:
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

    Keeps elements that are either actionable (have a pattern the executor
    can use) or meaningfully named - drops decorative/empty elements first,
    then caps the total count.
    """
    # Window-chrome controls (Close/Minimize/etc.) should never be offered
    # as options at all - a real near-miss in testing had the model
    # repeatedly clicking these out of confusion. Filtering them out here
    # is the primary defense; executor.py's is_dangerous check is the
    # backup in case one slips through some other path.
    from executor import is_dangerous

    elements = [e for e in screen_state["elements"] if not is_dangerous(e["name"])]

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

    kept = [_slim(e) for e in (actionable + named_only)[:max_elements]]
    return {
        "window_title": screen_state["window_title"],  # None = no current window yet
        "elements": kept,
        "open_windows": screen_state.get("open_windows", []),
    }


def _build_user_message(task, compact_screen, history):
    payload = json.dumps({
        "task": task,
        "current_screen": compact_screen,
        "actions_taken_so_far": history,
    }, separators=(",", ":"))  # no padding spaces - every token counts on a local model
    # The reminder is repeated here, AFTER the data, not just once in the
    # system prompt. Smaller models weight text near the end of the
    # context more heavily - and the screen's element objects (each with
    # their own "path" key) are otherwise the most recent, most prominent
    # JSON shape the model has seen, which is what it was latching onto.
    reminder = (
        "\n\nFirst check actions_taken_so_far: if they already completed the task, "
        "respond with action_type \"done\". "
        "Otherwise respond with exactly ONE JSON object describing the next "
        "ACTION to take - keys: reasoning, action_type, path, target_name, value. "
        "Do NOT return a screen element. Do NOT return a list. Do NOT plan "
        "multiple steps ahead. One action object, nothing else."
    )
    return payload + reminder


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


def get_action(task: str, screen_state: dict, history: list) -> dict:
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
    user_message = _build_user_message(task, compact_screen, history)
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
            action = _validate_action(parsed)  # catches valid-JSON-wrong-shape too
            action = _ground_action(action, compact_screen["elements"])
            return _check_action(action, compact_screen["elements"], history)
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
                "You must respond with exactly one JSON object with keys "
                "reasoning, action_type, path, target_name, value - describing ONE "
                "action. Do not return a screen element, do not return a "
                "list. Try again."
            )})

    raise ValueError(f"LLM returned invalid/malformed action after retries: {last_error}")