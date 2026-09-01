"""Scorer unit tests.

Written against unittest.TestCase so they run with zero installed packages
(`python -m unittest`) and also collect cleanly under pytest.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from scoring.score import DEFAULT_RUBRIC, score_job, score_jobs
from tests.helpers import NOW, criterion, job_by_id, load_fixture_jobs


class TestFixtureScores(unittest.TestCase):
    """Locked expectations for the shipped fixtures at a fixed instant."""

    EXPECTED = {
        "job-001": (100, True),
        "job-002": (95, True),
        "job-010": (89, True),
        "job-008": (80, True),
        "job-003": (56, False),
        "job-004": (0, False),
        "job-005": (0, False),
        "job-006": (0, False),
        "job-007": (0, False),
        "job-009": (0, False),
    }

    def test_every_fixture_scores_as_expected(self):
        results = {r["id"]: r for r in score_jobs(load_fixture_jobs(), None, NOW)}
        self.assertEqual(len(results), 10)
        for job_id, (score, passed) in self.EXPECTED.items():
            with self.subTest(job=job_id):
                self.assertEqual(results[job_id]["score"], score)
                self.assertEqual(results[job_id]["passed"], passed)

    def test_results_are_sorted_high_to_low(self):
        scores = [r["score"] for r in score_jobs(load_fixture_jobs(), None, NOW)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_breakdown_covers_every_weighted_criterion(self):
        result = score_job(job_by_id("job-001"), None, NOW)
        keys = [b["key"] for b in result["breakdown"]]
        self.assertEqual(set(keys), set(DEFAULT_RUBRIC["weights"].keys()))
        self.assertEqual(sum(b["maximum"] for b in result["breakdown"]), 100)

    def test_default_weights_sum_to_100(self):
        self.assertEqual(sum(DEFAULT_RUBRIC["weights"].values()), 100)


class TestHardGates(unittest.TestCase):
    """Each gate must zero the score AND say why in plain words."""

    def _disq(self, job):
        return score_job(job, None, NOW)

    def test_country_not_on_allowlist_disqualifies(self):
        result = self._disq(job_by_id("job-009"))
        self.assertTrue(result["disqualified"])
        self.assertEqual(result["score"], 0)
        self.assertIn("client country IN is not on the allowlist",
                      result["disqualifiers"])

    def test_blocklisted_country_disqualifies_even_if_allowlisted(self):
        job = job_by_id("job-001")
        rubric = {"client_country": {"allowlist": ["US"], "blocklist": ["US"]}}
        result = score_job(job, rubric, NOW)
        self.assertTrue(result["disqualified"])
        self.assertIn("client country US is on the blocklist",
                      result["disqualifiers"])

    def test_hire_rate_below_floor_disqualifies(self):
        result = self._disq(job_by_id("job-005"))
        self.assertIn("hire rate 18% is below the 40% floor",
                      result["disqualifiers"])

    def test_unverified_payment_disqualifies(self):
        result = self._disq(job_by_id("job-006"))
        self.assertIn("client payment method is not verified",
                      result["disqualifiers"])

    def test_budget_below_floor_disqualifies(self):
        result = self._disq(job_by_id("job-007"))
        self.assertIn("$120 fixed is below the $500 floor",
                      result["disqualifiers"])

    def test_banned_keyword_disqualifies(self):
        result = self._disq(job_by_id("job-004"))
        self.assertIn("banned keyword: equity only", result["disqualifiers"])

    def test_must_have_keyword_missing_disqualifies(self):
        job = job_by_id("job-003")  # WordPress job, no n8n
        result = score_job(job, {"keywords": {"must_have_any": ["n8n"]}}, NOW)
        self.assertIn("no must-have keyword present", result["disqualifiers"])

    def test_a_disqualified_job_keeps_its_raw_score_for_tuning(self):
        result = self._disq(job_by_id("job-004"))
        self.assertEqual(result["score"], 0)
        self.assertGreater(result["raw_score"], 70)

    def test_hard_gate_can_be_switched_off(self):
        job = job_by_id("job-005")  # 18% hire rate
        result = score_job(job, {"hire_rate": {"hard_gate": False}}, NOW)
        self.assertFalse(result["disqualified"])
        self.assertEqual(criterion(result, "hire_rate")["earned"], 0)
        self.assertGreater(result["score"], 0)

    def test_two_gates_report_two_reasons(self):
        result = self._disq(job_by_id("job-009"))
        self.assertEqual(len(result["disqualifiers"]), 2)


class TestCriteriaMath(unittest.TestCase):

    def test_freshness_decays_between_fresh_and_stale(self):
        job = job_by_id("job-001")
        job["posted_at"] = (NOW - timedelta(hours=25)).isoformat()
        earned = criterion(score_job(job, None, NOW), "freshness")["earned"]
        # 25h into a 2h..48h ramp -> (48-25)/(48-2) = 0.5 of 8 points
        self.assertAlmostEqual(earned, 4.0, places=2)

    def test_freshness_full_credit_inside_the_fresh_window(self):
        job = job_by_id("job-001")
        job["posted_at"] = (NOW - timedelta(minutes=30)).isoformat()
        self.assertEqual(criterion(score_job(job, None, NOW), "freshness")["earned"], 8)

    def test_freshness_zero_past_stale(self):
        job = job_by_id("job-001")
        job["posted_at"] = (NOW - timedelta(hours=200)).isoformat()
        self.assertEqual(criterion(score_job(job, None, NOW), "freshness")["earned"], 0)

    def test_future_posted_at_is_treated_as_brand_new(self):
        job = job_by_id("job-001")
        job["posted_at"] = (NOW + timedelta(hours=3)).isoformat()
        item = criterion(score_job(job, None, NOW), "freshness")
        self.assertEqual(item["earned"], 8)
        self.assertEqual(item["reason"], "posted 0.0h ago")

    def test_proposals_full_credit_at_or_below_ideal(self):
        job = job_by_id("job-001")
        job["proposals"] = 5
        self.assertEqual(criterion(score_job(job, None, NOW), "proposals")["earned"], 15)

    def test_proposals_decay_to_zero(self):
        job = job_by_id("job-001")
        job["proposals"] = 50
        self.assertEqual(criterion(score_job(job, None, NOW), "proposals")["earned"], 0)

    def test_hire_rate_accepts_percent_or_fraction(self):
        as_fraction = job_by_id("job-001")
        as_percent = job_by_id("job-001")
        as_percent["client"]["hire_rate"] = 82
        self.assertEqual(score_job(as_fraction, None, NOW)["score"],
                         score_job(as_percent, None, NOW)["score"])

    def test_hourly_uses_the_max_rate(self):
        job = job_by_id("job-002")
        item = criterion(score_job(job, None, NOW), "budget")
        self.assertEqual(item["reason"], "$85/hr")

    def test_score_is_zero_when_every_weight_is_zero(self):
        rubric = {"weights": {k: 0 for k in DEFAULT_RUBRIC["weights"]}}
        result = score_job(job_by_id("job-001"), rubric, NOW)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["breakdown"], [])

    def test_zero_weight_disables_a_criterion_entirely(self):
        job = job_by_id("job-009")  # normally disqualified on country
        rubric = {"weights": dict(DEFAULT_RUBRIC["weights"], client_country=0)}
        result = score_job(job, rubric, NOW)
        self.assertNotIn("client_country", [b["key"] for b in result["breakdown"]])
        self.assertNotIn("client country IN is not on the allowlist",
                         result["disqualifiers"])


class TestUnknownClientPolicy(unittest.TestCase):

    def test_neutral_gives_half_credit(self):
        result = score_job(job_by_id("job-008"), None, NOW)
        self.assertEqual(criterion(result, "hire_rate")["earned"], 7.5)
        self.assertEqual(criterion(result, "client_spend")["earned"], 6.0)
        self.assertFalse(result["disqualified"])

    def test_penalize_gives_zero_but_does_not_disqualify(self):
        result = score_job(job_by_id("job-008"),
                           {"unknown_client_policy": "penalize"}, NOW)
        self.assertEqual(criterion(result, "hire_rate")["earned"], 0)
        self.assertFalse(result["disqualified"])

    def test_disqualify_policy_rejects_unknown_clients(self):
        result = score_job(job_by_id("job-008"),
                           {"unknown_client_policy": "disqualify"}, NOW)
        self.assertTrue(result["disqualified"])
        self.assertEqual(result["score"], 0)


class TestMalformedInput(unittest.TestCase):
    """Real API payloads have holes in them. None of them may raise."""

    def test_empty_job_scores_without_raising(self):
        result = score_job({}, None, NOW)
        self.assertIsInstance(result["score"], int)
        self.assertEqual(len(result["breakdown"]), 8)

    def test_unparseable_timestamp_is_half_credit(self):
        job = job_by_id("job-001")
        job["posted_at"] = "not a date"
        self.assertEqual(criterion(score_job(job, None, NOW), "freshness")["earned"], 4.0)

    def test_missing_client_block_does_not_raise(self):
        job = job_by_id("job-001")
        del job["client"]
        result = score_job(job, None, NOW)
        self.assertIn("client country unknown - half credit",
                      [b["reason"] for b in result["breakdown"]])

    def test_partial_rubric_only_overrides_what_it_names(self):
        result = score_job(job_by_id("job-001"), {"alert_threshold": 95}, NOW)
        self.assertEqual(result["threshold"], 95)
        self.assertEqual(criterion(result, "budget")["maximum"], 20)

    def test_threshold_is_honoured(self):
        job = job_by_id("job-003")  # scores 56
        self.assertFalse(score_job(job, None, NOW)["passed"])
        self.assertTrue(score_job(job, {"alert_threshold": 50}, NOW)["passed"])

    def test_scoring_never_mutates_the_input_job(self):
        job = job_by_id("job-001")
        before = dict(job)
        score_job(job, None, NOW)
        self.assertEqual(job, before)


if __name__ == "__main__":
    unittest.main()
