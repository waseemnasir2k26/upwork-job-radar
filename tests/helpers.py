"""Shared test helpers. Plain stdlib - the suite has no third-party deps."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "jobs.sample.json")

# Every fixture-based assertion is scored against this instant so the numbers
# are stable forever instead of drifting with the wall clock.
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-01T12:00:00Z"


def load_fixture_jobs():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)["jobs"]


def job_by_id(job_id):
    for job in load_fixture_jobs():
        if job["id"] == job_id:
            return json.loads(json.dumps(job))  # deep copy
    raise KeyError(job_id)


def criterion(result, key):
    for item in result["breakdown"]:
        if item["key"] == key:
            return item
    raise KeyError(key)
