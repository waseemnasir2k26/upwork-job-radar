"""Python <-> JavaScript parity.

The n8n Code nodes run JavaScript. The reviewable, unit-tested engine is
Python. That is only honest if the two produce identical output, so these
tests drive both implementations over the same inputs and deep-compare every
number, boolean and reason string.

If Node is not installed the parity tests skip loudly rather than passing
quietly - a skipped parity test is a known gap, not a green light.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scoring.caps import CapExceeded, CapsGuard
from scoring.score import score_jobs
from tests.helpers import NOW, NOW_ISO, ROOT, load_fixture_jobs

DRIVER = os.path.join(ROOT, "tests", "parity_driver.js")
NODE = shutil.which("node")


def _run_driver(*args) -> object:
    proc = subprocess.run([NODE, DRIVER] + list(args),
                          capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise AssertionError("parity driver failed:\n" + proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node is not installed - parity cannot be proven")
class TestScorerParity(unittest.TestCase):

    def _compare(self, jobs, rubric=None, now_iso=NOW_ISO, now=NOW):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump({"jobs": jobs, "rubric": rubric}, handle)
            path = handle.name
        try:
            js_results = _run_driver("score", path, now_iso)
        finally:
            os.unlink(path)
        py_results = json.loads(json.dumps(score_jobs(jobs, rubric, now)))
        self.assertEqual(len(py_results), len(js_results))
        for py, js in zip(py_results, js_results):
            self.assertEqual(py, js, "divergence on job " + str(py.get("id")))
        return py_results

    def test_all_fixtures_match_exactly(self):
        results = self._compare(load_fixture_jobs())
        self.assertEqual(len(results), 10)

    def test_tuned_rubric_matches_exactly(self):
        rubric = {
            "alert_threshold": 55,
            "unknown_client_policy": "penalize",
            "weights": {"client_country": 5, "hire_rate": 25, "client_spend": 5,
                        "payment_verified": 10, "budget": 25, "proposals": 10,
                        "keywords": 15, "freshness": 5},
            "keywords": {"must_have_any": ["n8n", "automation"]},
            "budget": {"fixed_floor_usd": 1000, "hourly_floor_usd": 50},
            "client_country": {"allowlist": ["US", "GB"], "hard_gate": True},
        }
        self._compare(load_fixture_jobs(), rubric)

    def test_edge_case_jobs_match_exactly(self):
        stale = (NOW - timedelta(hours=100)).isoformat()
        future = (NOW + timedelta(hours=5)).isoformat()
        edge_jobs = [
            {"id": "empty"},
            {"id": "no-client", "title": "n8n automation", "budget": 1500,
             "proposals": 0, "posted_at": NOW_ISO},
            {"id": "bad-date", "title": "automation", "posted_at": "not a date",
             "budget": 800, "client": {"country": "US", "hire_rate": 0.5,
                                       "total_spend": 900,
                                       "payment_verified": True}},
            {"id": "stale", "title": "n8n workflow", "posted_at": stale,
             "budget": 5000, "proposals": 99,
             "client": {"country": "US", "hire_rate": 1, "total_spend": 999999,
                        "payment_verified": True}},
            {"id": "future", "title": "webhook automation", "posted_at": future,
             "job_type": "hourly", "hourly_min": 40, "hourly_max": None,
             "proposals": None,
             "client": {"country": "gb", "hire_rate": 55, "total_spend": 0,
                        "payment_verified": None}},
            {"id": "zero-budget", "title": "shopify workflow", "job_type": "fixed",
             "budget": 0, "proposals": 5, "posted_at": NOW_ISO,
             "client": {"country": "US", "hire_rate": 0.4, "total_spend": 10000,
                        "payment_verified": True}},
        ]
        self._compare(edge_jobs)

    def test_zeroed_weights_match_exactly(self):
        rubric = {"weights": {"client_country": 0, "hire_rate": 0,
                              "client_spend": 0, "payment_verified": 0,
                              "budget": 0, "proposals": 0, "keywords": 0,
                              "freshness": 0}}
        self._compare(load_fixture_jobs(), rubric)


@unittest.skipIf(NODE is None, "node is not installed - parity cannot be proven")
class TestCapsParity(unittest.TestCase):

    CAPS = {
        "max_steps_per_run": 3,
        "wall_clock_seconds": 60,
        "spend": {"cap_usd": 1.0, "window_hours": 24},
        "circuit_breaker": {"consecutive_errors": 2, "cooldown_minutes": 30,
                            "auto_disable": True},
    }

    OPS = [
        {"op": "start_run"},
        {"op": "check", "cost": 0.3}, {"op": "record_step", "cost": 0.3},
        {"op": "check", "cost": 0.3}, {"op": "record_step", "cost": 0.3},
        {"op": "check", "cost": 0.3}, {"op": "record_step", "cost": 0.3},
        {"op": "check", "cost": 0.3},                       # step cap
        {"op": "record_error", "message": "step cap"},
        {"op": "record_error", "message": "again"},         # trips the breaker
        {"op": "start_run"},                                # refused
        {"op": "advance", "seconds": 1860},                 # 31 minutes
        {"op": "start_run"},                                # half-open probe
        {"op": "record_error", "message": "probe failed"},  # re-trips
        {"op": "advance", "seconds": 1860},
        {"op": "start_run"},
        {"op": "record_success"},
        {"op": "advance", "seconds": 90000},                # 25h, window rolls
        {"op": "start_run"},
        {"op": "check", "cost": 0.9},
        {"op": "reset"},
    ]

    def _python_trace(self):
        start = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock = {"now": start}
        guard = CapsGuard(self.CAPS, {}, now_fn=lambda: clock["now"])
        trace = []
        for op in self.OPS:
            entry = {"op": op["op"], "error": None}
            try:
                name = op["op"]
                if name == "advance":
                    clock["now"] = clock["now"] + timedelta(seconds=op["seconds"])
                elif name == "start_run":
                    guard.start_run()
                elif name == "check":
                    guard.check(op.get("cost", 0))
                elif name == "record_step":
                    guard.record_step(op.get("cost", 0))
                elif name == "record_error":
                    entry["tripped"] = guard.record_error(op.get("message", ""))
                elif name == "record_success":
                    guard.record_success()
                elif name == "reset":
                    guard.reset()
                else:
                    raise AssertionError("unknown op " + name)
            except CapExceeded as exc:
                entry["error"] = {"cap": exc.cap, "message": exc.message}
            entry["state"] = guard.to_dict()
            entry["remaining"] = guard.remaining()
            trace.append(entry)
        return json.loads(json.dumps(trace))

    def test_scripted_run_matches_exactly(self):
        script = {"start": "2026-09-01T12:00:00.000Z", "caps": self.CAPS,
                  "state": {}, "ops": self.OPS}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump(script, handle)
            path = handle.name
        try:
            js_trace = _run_driver("caps", path)
        finally:
            os.unlink(path)
        py_trace = self._python_trace()
        self.assertEqual(len(py_trace), len(js_trace))
        for index, (py, js) in enumerate(zip(py_trace, js_trace)):
            self.assertEqual(py, js,
                             "divergence at op " + str(index) + " (" + py["op"] + ")")

    def test_the_script_actually_exercises_the_interesting_paths(self):
        """A parity test that never trips anything proves nothing."""
        trace = self._python_trace()
        caps_hit = {e["error"]["cap"] for e in trace if e["error"]}
        self.assertIn("max_steps_per_run", caps_hit)
        self.assertIn("circuit_breaker", caps_hit)
        self.assertTrue(any(e.get("tripped") for e in trace))
        self.assertTrue(any(e["state"]["half_open"] for e in trace))


class TestNodePresence(unittest.TestCase):

    def test_node_is_available_or_the_gap_is_explicit(self):
        if NODE is None:
            self.skipTest("node is not installed; JS parity was NOT verified")
        self.assertTrue(os.path.exists(DRIVER))


if __name__ == "__main__":
    unittest.main()
