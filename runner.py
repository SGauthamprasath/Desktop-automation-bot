"""
runner.py
---------
Benchmark harness: runs every task in tasks.json through the agent loop,
then checks the REAL result instead of trusting the agent's "done".

For each task:
  1. run its "setup" steps (fixtures.py) - open the scratch file, reset a
     setting, clear Calculator, ...
  2. run the agent
  3. run its "verify" checks (verify.py) against the live UI
  4. look for side effects: dialogs left open in windows the agent acted
     in, and windows it opened that the task didn't allow
  5. clean up (cancel dialogs, close menus, close new windows) and run the
     task's "teardown" steps, so the next task starts from a known state

A task is VERIFIED only if every check passes and there are no side
effects. Tasks without "verify" are reported on the agent's claim alone.

tasks.json is {"workspace": {files...}, "tasks": [...]}; a plain list of
tasks still works. The workspace is rebuilt in the temp directory each run.

Run all:        python runner.py
Run some:       python runner.py --only calc_open_and_add notepad_set_value
Filter by tag:  python runner.py --category safety
"""

import argparse
import csv
import json
import os
import sys
import traceback
from datetime import datetime

import fixtures
import verify
from agent import run_task

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def load_tasks(path="tasks.json"):
    """Returns (workspace_files, tasks)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {}, data
    return data.get("workspace", {}), data["tasks"]


def run_one(task) -> dict:
    row = {
        "task_id": task["id"],
        "category": task.get("category", ""),
        "app": task.get("app_title_regex", ""),
        "expected_action_type": task.get("expected_action_type", ""),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "success": False, "verified": "", "steps": -1, "action_types_used": "",
        "final_window": "", "checks": "", "side_effects": "", "error": "",
    }
    before, touched = {}, []
    try:
        try:
            fixtures.run_steps(task.get("setup"))
        except Exception as e:
            raise SetupError(f"{type(e).__name__}: {e}") from e
        before = fixtures.snapshot_windows()
        outcome = run_task(task["instruction"], task.get("app_title_regex"))
        touched = outcome.get("touched_windows", [])
        row.update(
            success=outcome["success"], steps=outcome["steps"],
            final_window=outcome.get("final_window") or "",
            # Which action types got used - confirms the task exercised what it was meant to.
            action_types_used=",".join(h["action"]["action_type"] for h in outcome["history"]),
        )
        if "verify" in task:
            results = verify.run_checks(task["verify"], outcome)
            side_effects = fixtures.detect_side_effects(before, touched, task.get("allow_new_window"))
            row["checks"] = " | ".join(f"{'ok' if ok else 'FAIL'}: {ev}" for ok, ev in results)
            row["side_effects"] = " | ".join(side_effects)
            row["verified"] = all(ok for ok, _ in results) and not side_effects
            for ok, evidence in results:
                print(f"  [check] {'ok  ' if ok else 'FAIL'} {evidence}")
            for problem in side_effects:
                print(f"  [side effect] {problem}")
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        row["verified"] = False if "verify" in task else ""
        print(f"\n[CRASH] {task['id']}: {e}")
        traceback.print_exc()
    finally:
        try:
            fixtures.cleanup(before, touched)
            fixtures.run_steps(task.get("teardown"))
        except Exception as e:
            print(f"  [cleanup issue] {type(e).__name__}: {e}")
    return row


class SetupError(Exception):
    """A task's setup failed - the agent never ran."""


# Stop after this many setup failures in a row. When Notepad crashed on every
# start, each task relaunched it 3 times and all 17 Notepad tasks "ran" anyway.
MAX_SETUP_FAILURES = 2


def run_all(tasks, only_ids=None, category=None):
    if only_ids:
        tasks = [t for t in tasks if t["id"] in only_ids]
    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    if not tasks:
        print("No matching tasks")
        return []

    results = []
    setup_failures = 0
    for task in tasks:
        print(f"\n{'=' * 60}")
        print(f"TASK: {task['id']}  ({task.get('notes', '')})")
        print(f"  instruction: {task['instruction']}")
        print(f"  app: {task.get('app_title_regex') or '(none - agent opens/switches itself)'}")
        print("=" * 60)
        row = run_one(task)
        results.append(row)
        setup_failures = setup_failures + 1 if row["error"].startswith("SetupError") else 0
        if setup_failures >= MAX_SETUP_FAILURES:
            print(f"\n[STOP] setup failed for {setup_failures} tasks in a row ({row['error']}) - an app "
                  f"the tasks need is probably not starting. Fix it and rerun; the remaining tasks were skipped.")
            break
    return results


def save_results(results):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    fieldnames = ["task_id", "category", "app", "expected_action_type", "action_types_used",
                  "success", "verified", "steps", "final_window", "checks", "side_effects",
                  "error", "timestamp"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nResults saved to: {path}")
    return path


def print_summary(results):
    print(f"\n{'=' * 60}")
    print("SUMMARY  (agent claim vs verified result)")
    print("=" * 60)
    for r in results:
        claimed = "done" if r["success"] else "fail"
        status = {True: "PASS", False: "FAIL", "": "----"}[r["verified"]]
        print(f"  [{status}] {r['task_id']:32} agent said {claimed:4}  steps={r['steps']}")
    checked = [r for r in results if r["verified"] != ""]
    print(f"\nverified: {sum(1 for r in checked if r['verified'])}/{len(checked)}"
          f"   (agent claimed success on {sum(1 for r in results if r['success'])}/{len(results)})")


if __name__ == "__main__":
    # The Windows console/pipe default (cp1252) can't print names like "√"
    # (Calculator's square root button) - a print crashed a whole task.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", help="Run only these task ids")
    parser.add_argument("--category", help="Run only tasks with this category")
    parser.add_argument("--tasks-file", default="tasks.json")
    args = parser.parse_args()

    workspace, tasks = load_tasks(args.tasks_file)
    fixtures.build_workspace(workspace)
    results = run_all(tasks, only_ids=args.only, category=args.category)
    if results:
        save_results(results)
        print_summary(results)
