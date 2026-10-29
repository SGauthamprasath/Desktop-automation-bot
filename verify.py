"""
verify.py
---------
Per-task checks for runner.py. Each task in tasks.json lists the checks
that define success; a task only passes when ALL of them hold, measured on
the real UI after the agent finishes - not when the agent claims "done"
(in testing the agent said "done" on tasks it had visibly not completed).

Check types:
    {"element": {"window": <title regex>, <element spec>, "menu": <menu to open first>},
     "property": "name" | "value" | "toggled" | "selected" | "expanded" | "enabled" | "scroll_percent",
     "equals": x | "contains": "text" | "min": n}
    {"final_window": <regex>}      the agent ended in this window
    {"window_exists": <regex>}
    {"no_window": <regex>}         e.g. a blocked app must not be open
    {"agent_success": false}       e.g. a refused task must not be reported as done

Element spec keys (see fixtures.find_element): name, name_contains,
automation_id, control_type.
"""

import re

from fixtures import expand, find_element, find_window, menu_open, top_windows


def _read(element, prop):
    if prop == "name":
        return element.element_info.name
    if prop == "value":
        from executor import _normalize_text
        return _normalize_text(element.iface_value.CurrentValue)
    if prop == "toggled":
        return element.iface_toggle.CurrentToggleState == 1
    if prop == "selected":
        return bool(element.iface_selection_item.CurrentIsSelected)
    if prop == "expanded":
        return element.iface_expand_collapse.CurrentExpandCollapseState == 1
    if prop == "enabled":
        return element.is_enabled()
    if prop == "scroll_percent":
        return element.iface_scroll.CurrentVerticalScrollPercent
    raise ValueError(f"unknown property {prop!r}")


def _compare(actual, spec) -> bool:
    if "equals" in spec:
        return actual == spec["equals"]
    if "contains" in spec:
        return str(spec["contains"]).lower() in str(actual).lower()
    if "min" in spec:
        return actual is not None and actual >= spec["min"]
    raise ValueError("check needs one of equals / contains / min")


def run_check(check: dict, outcome: dict) -> tuple:
    """Returns (passed, evidence)."""
    check = expand(check)
    try:
        if "element" in check:
            target = check["element"]
            window = find_window(target["window"], timeout=5)
            with menu_open(window, target.get("menu")):
                actual = _read(find_element(window, target), check["property"])
            label = target.get("name") or target.get("automation_id") or target.get("name_contains")
            return _compare(actual, check), f"{label}.{check['property']} = {actual!r}"
        if "final_window" in check:
            final = outcome.get("final_window") or ""
            return bool(re.search(check["final_window"], final)), f"final window {final!r}"
        if "window_exists" in check:
            titles = [w.window_text() for w in top_windows()]
            return any(re.search(check["window_exists"], t) for t in titles), f"window {check['window_exists']!r}"
        if "no_window" in check:
            found = [w.window_text() for w in top_windows() if re.search(check["no_window"], w.window_text())]
            return not found, f"matching windows: {found}"
        if "agent_success" in check:
            return outcome.get("success") == check["agent_success"], f"agent success={outcome.get('success')}"
    except Exception as e:
        return False, f"check could not run: {type(e).__name__}: {e}"
    return False, f"unknown check {check}"


def run_checks(checks: list, outcome: dict) -> list:
    return [run_check(c, outcome) for c in checks or []]
