"""
runner.py
---------
Loops through tasks.json, runs each task through the existing agent loop,
and logs results. This is deliberately the seed of your Phase 4 benchmark
harness - the same tasks.json structure (id, instruction, app, notes) is
what you'll extend later with "category": "ambiguous" / "failure_injected"
and pass/fail verification logic.

Run with: python runner.py
Run a single task only: python runner.py --only notepad_toggle_wordwrap
"""

import argparse
import csv
import json
import os
import traceback
from datetime import datetime

from agent import run_task

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def load_tasks(path="tasks.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_all(tasks, only_id=None):
    if only_id:
        tasks = [t for t in tasks if t["id"] == only_id]
        if not tasks:
            print(f"No task found with id '{only_id}'")
            return []

    results = []
    for task in tasks:
        print(f"\n{'=' * 60}")
        print(f"TASK: {task['id']}  ({task.get('notes', '')})")
        print(f"  instruction: {task['instruction']}")
        print(f"  app: {task['app_title_regex']}")
        print("=" * 60)

        row = {
            "task_id": task["id"],
            "app": task["app_title_regex"],
            "expected_action_type": task.get("expected_action_type", ""),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        try:
            outcome = run_task(task["instruction"], task["app_title_regex"])
            row["success"] = outcome["success"]
            row["steps"] = outcome["steps"]
            row["error"] = ""
            # Which action_types actually got used - lets you confirm the
            # task exercised the pattern you intended, not just "it finished".
            used_types = [h["action"]["action_type"] for h in outcome["history"]]
            row["action_types_used"] = ",".join(used_types)
        except Exception as e:
            row["success"] = False
            row["steps"] = -1
            row["error"] = str(e)
            row["action_types_used"] = ""
            print(f"\n[CRASH] {task['id']}: {e}")
            traceback.print_exc()

        results.append(row)

    return results


def save_results(results):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    fieldnames = ["task_id", "app", "expected_action_type", "action_types_used",
                  "success", "steps", "error", "timestamp"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nResults saved to: {path}")
    return path


def print_summary(results):
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for r in results if r["success"])
    for r in results:
        status = "PASS" if r["success"] else "FAIL"
        matched = ""
        if r["expected_action_type"] and r["expected_action_type"] not in r["action_types_used"]:
            matched = "  [expected action_type never used - check manually]"
        print(f"  [{status}] {r['task_id']}  (steps={r['steps']}){matched}")
    print(f"\n{passed}/{len(results)} tasks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Run a single task by id")
    parser.add_argument("--tasks-file", default="tasks.json")
    args = parser.parse_args()

    tasks = load_tasks(args.tasks_file)
    results = run_all(tasks, only_id=args.only)
    if results:
        save_results(results)
        print_summary(results)
