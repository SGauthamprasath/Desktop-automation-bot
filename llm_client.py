"""
llm_client.py
-------------
Wraps both backends behind one function, get_action(), so agent.py's
loop doesn't need to know or care which model is actually deciding
the action. This is also where the Phase 2 confidence gate will plug
in later - it wraps a call to get_action(), regardless of backend.
"""

import json
import config

SYSTEM_PROMPT = """You are a desktop automation agent. You are given:
1. A task instruction from the user.
2. The current UI state as a structured list of elements (from Windows UI Automation), each with a "path" you must use to reference it.

Decide the SINGLE next action to take. Respond with ONLY a JSON object, no other text, in this exact shape:

{
  "action_type": "invoke" | "set_value" | "toggle" | "click_fallback" | "done",
  "path": [<indices>],
  "value": "<text>",
  "reasoning": "<one short sentence on why this action>"
}

Use "invoke" for buttons/menu items. Use "set_value" for text fields.
Use "click_fallback" ONLY if no element has an "invoke" or "value" pattern for what you need to do.
Use "done" once the task is complete - no path needed, omit "value" if unused.

Check "actions_taken_so_far" before deciding: if you already successfully
performed the action the task needs, respond with action_type "done" instead
of repeating it.

Respond with ONLY that single JSON object - one object, the five keys shown above,
nothing else. NEVER return a list of actions, NEVER wrap it in an "actions" key,
NEVER echo back the input. Decide ONE action only, even if several elements look
relevant - you will be asked again next step.

Example of a correct response, given a screen with a button at path [1, 2]:
{"action_type": "invoke", "path": [1, 2], "value": "", "reasoning": "Opens the Edit menu"}

Respond with ONLY the JSON object. No markdown fences, no commentary before or after it.
"""

REQUIRED_KEYS = {"action_type", "path", "value", "reasoning"}
VALID_ACTION_TYPES = {"invoke", "set_value", "toggle", "click_fallback", "done"}


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
    if parsed["action_type"] not in VALID_ACTION_TYPES:
        raise ActionSchemaError(f"action_type '{parsed['action_type']}' is not one of {VALID_ACTION_TYPES}")
    if parsed["action_type"] != "done" and "path" not in parsed:
        raise ActionSchemaError("missing required key 'path' (required unless action_type is 'done')")
    return parsed


def _task_relevance(element: dict, task_words: set) -> int:
    """Crude relevance score: how many task words appear in this
    element's name. Elements matching the task get sorted ahead of
    generic toolbar clutter instead of being lost past the cap - this
    is what let the model miss the Word Wrap checkbox and a named
    folder in testing, both present but outranked by earlier, more
    generic elements."""
    name_words = set(element["name"].lower().split())
    return len(task_words & name_words)


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

    Keeps elements that are either actionable (have a pattern) or
    meaningfully named - drops decorative/empty elements first, then
    caps the total count.
    """
    # Window-chrome controls (Close/Minimize/etc.) should never be offered
    # as options at all - a real near-miss in testing had the model
    # repeatedly clicking these out of confusion. Filtering them out here
    # is the primary defense; executor.py's DANGEROUS_NAMES check is the
    # backup in case one slips through some other path.
    from executor import DANGEROUS_NAMES

    elements = screen_state["elements"]
    elements = [e for e in elements if e["name"].strip().lower() not in DANGEROUS_NAMES]

    task_words = set(task.lower().split())
    actionable = [e for e in elements if e["patterns"]]
    named_only = [e for e in elements if not e["patterns"] and e["name"].strip()]
    actionable.sort(key=lambda e: _task_relevance(e, task_words), reverse=True)

    kept = (actionable + named_only)[:max_elements]
    return {"window_title": screen_state["window_title"], "elements": kept}


def _build_user_message(task, screen_state, history):
    payload = json.dumps({
        "task": task,
        "current_screen": _compact_elements(screen_state, task=task),
        "actions_taken_so_far": history,
    })
    # The reminder is repeated here, AFTER the data, not just once in the
    # system prompt. Smaller models weight text near the end of the
    # context more heavily - and the screen's element objects (each with
    # their own "path" key) are otherwise the most recent, most prominent
    # JSON shape the model has seen, which is what it was latching onto.
    reminder = (
        "\n\nRespond now with exactly ONE JSON object describing the next "
        "ACTION to take - keys: action_type, path, value, reasoning. "
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
    # Ollama exposes an OpenAI-compatible endpoint, so the standard
    # openai client works unmodified - just pointed at a local URL.
    from openai import OpenAI
    client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key="ollama")  # api_key is unused but required by the client

    response = client.chat.completions.create(
        model=config.LOCAL_MODEL,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},  # Ollama: forces syntactically valid JSON
    )
    return response.choices[0].message.content


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
    user_message = _build_user_message(task, screen_state, history)
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
            return _validate_action(parsed)  # catches valid-JSON-wrong-shape too
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
                "action_type, path, value, reasoning - describing ONE "
                "action. Do not return a screen element, do not return a "
                "list. Try again."
            )})

    raise ValueError(f"LLM returned invalid/malformed action after retries: {last_error}")