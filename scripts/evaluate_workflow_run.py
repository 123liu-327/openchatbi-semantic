"""Inspect persisted stage evidence and optionally compare it with explicit gold outputs."""

import argparse
import json
from collections import Counter
from pathlib import Path


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and subset(actual[key], value) for key, value in expected.items()
        )
    return actual == expected


def evaluate_run(path, gold=None):
    path = Path(path)
    if path.is_dir():
        path = path / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    safety, authorization, violations = set(), set(), []
    for event in events:
        key = (event["task_id"], event["inputs"].get("sql"))
        if event["node"] == "review_sql" and event["status"] == "ok":
            safety.add(key)
        if event["node"] == "authorize_user" and event["status"] == "ok":
            authorization.add(key)
        if event["node"] == "execute_sql" and (key not in safety or key not in authorization):
            violations.append(
                {"event_id": event["event_id"], "reason": "Execution missing matching safety/authorization evidence"}
            )
    comparisons = []
    for check in gold or []:
        matches = [event for event in events if event["node"] == check["node"] and event["task_id"] == check["task_id"]]
        passed = bool(matches) and subset(matches[-1]["outputs"], check.get("outputs", {}))
        passed = passed and ("status" not in check or matches[-1]["status"] == check["status"])
        comparisons.append({"node": check["node"], "task_id": check["task_id"], "passed": passed})
    return {
        "events": len(events),
        "node_counts": dict(Counter(event["node"] for event in events)),
        "status_counts": dict(Counter(event["status"] for event in events)),
        "execution_violations": violations,
        "gold_comparisons": comparisons,
        "gold_evaluated": gold is not None,
        "passed": not violations and all(check["passed"] for check in comparisons),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Run directory or events.jsonl path")
    parser.add_argument("--gold", type=Path, help="JSON list: task_id/node/outputs subset/status")
    args = parser.parse_args()
    result = evaluate_run(args.run, json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
