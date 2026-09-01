"""End-to-end CLI test: the documented quick-start command must actually work."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.helpers import NOW_ISO, ROOT

FIXTURE = os.path.join("tests", "fixtures", "jobs.sample.json")


def run_cli(*extra):
    return subprocess.run(
        [sys.executable, "-m", "scoring.cli", "--jobs", FIXTURE,
         "--now", NOW_ISO] + list(extra),
        capture_output=True, text=True, cwd=ROOT)


class TestCli(unittest.TestCase):

    def test_runs_clean_with_zero_config(self):
        proc = run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("UPWORK JOB RADAR", proc.stdout)

    def test_prints_the_four_passing_jobs_and_the_digest(self):
        out = run_cli().stdout
        self.assertEqual(out.count("draft opener (not sent):"), 4)
        self.assertIn("10 scored | 4 passed the threshold | 5 disqualified", out)

    def test_all_flag_shows_the_rejects_too(self):
        out = run_cli("--all").stdout
        self.assertIn("DISQUALIFIED:", out)
        self.assertIn("client payment method is not verified", out)

    def test_json_output_is_machine_readable(self):
        proc = run_cli("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = json.loads(proc.stdout)
        self.assertEqual(len(results), 10)
        self.assertEqual(results[0]["score"], 100)

    def test_the_example_rubric_file_loads_and_runs(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        proc = run_cli("--rubric", os.path.join("config", "rubric.example.yaml"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("UPWORK JOB RADAR", proc.stdout)

    def test_output_never_claims_to_have_applied(self):
        out = run_cli("--all").stdout.lower()
        for banned in ("proposal submitted", "applied to", "connects spent"):
            self.assertNotIn(banned, out)


class TestExampleConfigs(unittest.TestCase):
    """The shipped examples must be exactly the built-in defaults.

    A README that documents one set of numbers while the code uses another is
    worse than no README.
    """

    def setUp(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")

    def test_rubric_example_matches_the_built_in_defaults(self):
        from scoring.config import load_config_file
        from scoring.score import DEFAULT_RUBRIC
        loaded = load_config_file(os.path.join(ROOT, "config", "rubric.example.yaml"))
        self.assertEqual(loaded, DEFAULT_RUBRIC)

    def test_caps_example_matches_the_built_in_defaults(self):
        from scoring.caps import DEFAULT_CAPS
        from scoring.config import load_config_file
        loaded = load_config_file(os.path.join(ROOT, "config", "caps.example.yaml"))
        self.assertEqual(loaded, DEFAULT_CAPS)


if __name__ == "__main__":
    unittest.main()
