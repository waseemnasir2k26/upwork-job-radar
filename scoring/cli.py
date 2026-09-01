"""Command-line runner: score a JSON file of jobs and print the alerts.

    python -m scoring.cli --jobs tests/fixtures/jobs.sample.json

Add --rubric config/rubric.example.yaml to use a tuned rubric, --all to print
the jobs that failed too (useful when tuning), --json for machine output.

This never calls Upwork. Feed it a file that something else fetched.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from .config import load_rubric
from .score import score_jobs
from .slack import draft_opener, render_digest, render_text_alert


def _load_jobs(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("jobs", [])
    if not isinstance(data, list):
        raise ValueError(path + " must contain a list of jobs, or {\"jobs\": [...]}")
    return data


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scoring.cli",
                                     description="Score Upwork jobs offline.")
    parser.add_argument("--jobs", required=True,
                        help="path to a JSON file of job objects")
    parser.add_argument("--rubric", default=None,
                        help="path to a rubric .yaml/.json (defaults are built in)")
    parser.add_argument("--now", default=None,
                        help="ISO timestamp to score against (default: now, UTC)")
    parser.add_argument("--all", action="store_true",
                        help="also print jobs that did not pass")
    parser.add_argument("--json", action="store_true",
                        help="print raw JSON results instead of text alerts")
    args = parser.parse_args(argv)

    jobs = _load_jobs(args.jobs)
    rubric = load_rubric(args.rubric)
    now = datetime.now(timezone.utc)
    if args.now:
        text = args.now[:-1] + "+00:00" if args.now.endswith("Z") else args.now
        now = datetime.fromisoformat(text)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    results = score_jobs(jobs, rubric, now=now)
    by_id = {job.get("id"): job for job in jobs}

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    shown = 0
    for result in results:
        if not result.get("passed") and not args.all:
            continue
        job = by_id.get(result.get("id"), {})
        print(render_text_alert(result, job))
        if result.get("passed"):
            print("")
            print("draft opener (not sent):")
            print("  " + draft_opener(job, result).replace("\n", "\n  "))
        print("")
        print("=" * 62)
        print("")
        shown += 1

    if shown == 0:
        print("No job passed the threshold. Re-run with --all to see why.")
        print("")
    print(render_digest(results))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
