"""Alert rendering tests, including the safety wording that must never vanish."""

from __future__ import annotations

import json
import unittest

from scoring.score import score_job, score_jobs
from scoring.slack import (build_slack_blocks, draft_opener, render_digest,
                           render_text_alert)
from tests.helpers import NOW, job_by_id, load_fixture_jobs


class TestTextAlert(unittest.TestCase):

    def setUp(self):
        self.job = job_by_id("job-001")
        self.result = score_job(self.job, None, NOW)
        self.text = render_text_alert(self.result, self.job)

    def test_shows_score_and_threshold(self):
        self.assertIn("score 100/100", self.text)
        self.assertIn("alert at 70+", self.text)

    def test_shows_every_criterion_with_a_reason(self):
        for item in self.result["breakdown"]:
            self.assertIn(item["key"], self.text)
            self.assertIn(item["reason"], self.text)

    def test_shows_the_client_facts_a_human_needs(self):
        self.assertIn("82%", self.text)
        self.assertIn("$46,000", self.text)
        self.assertIn("payment verified", self.text)
        self.assertIn("3 proposals", self.text)

    def test_carries_the_read_only_disclaimer(self):
        self.assertIn("nothing was sent to Upwork", self.text)
        self.assertIn("apply manually", self.text)

    def test_links_to_the_job(self):
        self.assertIn(self.job["url"], self.text)

    def test_disqualified_job_lists_its_reasons(self):
        job = job_by_id("job-006")
        text = render_text_alert(score_job(job, None, NOW), job)
        self.assertIn("DISQUALIFIED:", text)
        self.assertIn("client payment method is not verified", text)

    def test_hourly_job_renders_a_range(self):
        job = job_by_id("job-002")
        text = render_text_alert(score_job(job, None, NOW), job)
        self.assertIn("$55-$85/hr", text)

    def test_missing_fields_do_not_raise(self):
        result = score_job({}, None, NOW)
        text = render_text_alert(result, {})
        self.assertIn("(untitled)", text)


class TestSlackBlocks(unittest.TestCase):

    def setUp(self):
        self.job = job_by_id("job-001")
        self.result = score_job(self.job, None, NOW)
        self.payload = build_slack_blocks(
            self.result, self.job, draft_opener(self.job, self.result))

    def test_payload_is_json_serialisable(self):
        json.dumps(self.payload)

    def test_has_fallback_text_and_blocks(self):
        self.assertIn("text", self.payload)
        self.assertIsInstance(self.payload["blocks"], list)
        self.assertTrue(self.payload["blocks"])

    def test_respects_slacks_50_block_limit(self):
        self.assertLessEqual(len(self.payload["blocks"]), 50)

    def test_header_respects_slacks_150_char_limit(self):
        header = self.payload["blocks"][0]
        self.assertEqual(header["type"], "header")
        self.assertLessEqual(len(header["text"]["text"]), 150)

    def test_long_titles_are_truncated_in_the_header(self):
        job = job_by_id("job-001")
        job["title"] = "x" * 400
        payload = build_slack_blocks(score_job(job, None, NOW), job)
        self.assertLessEqual(len(payload["blocks"][0]["text"]["text"]), 150)

    def test_every_block_has_a_type(self):
        for block in self.payload["blocks"]:
            self.assertIn("type", block)

    def test_carries_the_read_only_disclaimer(self):
        blob = json.dumps(self.payload)
        self.assertIn("Nothing was submitted to Upwork", blob)

    def test_the_opener_is_labelled_as_a_draft(self):
        blob = json.dumps(self.payload)
        self.assertIn("Draft opener", blob)
        self.assertIn("nothing was sent", blob)

    def test_opener_is_omitted_when_not_supplied(self):
        payload = build_slack_blocks(self.result, self.job)
        self.assertNotIn("Draft opener", json.dumps(payload))


class TestDraftOpener(unittest.TestCase):

    def test_is_deterministic(self):
        job = job_by_id("job-001")
        result = score_job(job, None, NOW)
        self.assertEqual(draft_opener(job, result), draft_opener(job, result))

    def test_mentions_the_job_title_and_asks_questions(self):
        job = job_by_id("job-001")
        opener = draft_opener(job, score_job(job, None, NOW))
        self.assertIn(job["title"], opener)
        self.assertIn("?", opener)

    def test_never_claims_to_have_applied(self):
        for job in load_fixture_jobs():
            opener = draft_opener(job, score_job(job, None, NOW)).lower()
            for banned in ("i have applied", "proposal submitted", "i submitted",
                           "i sent a proposal", "connects spent"):
                self.assertNotIn(banned, opener)

    def test_handles_a_job_with_no_skills(self):
        job = job_by_id("job-001")
        job["skills"] = []
        opener = draft_opener(job, score_job(job, None, NOW))
        self.assertIn("the stack you listed", opener)


class TestDigest(unittest.TestCase):

    def setUp(self):
        self.results = score_jobs(load_fixture_jobs(), None, NOW)

    def test_counts_are_correct(self):
        digest = render_digest(self.results)
        self.assertIn("10 scored | 4 passed the threshold | 5 disqualified",
                      digest)

    def test_lists_the_passing_jobs(self):
        digest = render_digest(self.results)
        self.assertIn("n8n workflow to sync orders into our warehouse API"[:56],
                      digest)

    def test_groups_disqualifier_reasons(self):
        digest = render_digest(self.results)
        self.assertIn("top disqualifier reasons:", digest)
        self.assertIn("banned keyword", digest)

    def test_an_empty_day_is_stated_plainly_not_hidden(self):
        digest = render_digest([])
        self.assertIn("0 scored", digest)
        self.assertIn("normal day, not a broken radar", digest)

    def test_includes_caps_headroom_when_given(self):
        digest = render_digest(self.results, {
            "steps_used": 10, "steps_cap": 200, "spend_used_usd": 0.0,
            "spend_cap_usd": 5.0, "disabled": False})
        self.assertIn("10/200 steps", digest)
        self.assertIn("breaker closed", digest)

    def test_shows_an_open_breaker(self):
        digest = render_digest(self.results, {
            "steps_used": 0, "steps_cap": 200, "spend_used_usd": 5.0,
            "spend_cap_usd": 5.0, "disabled": True})
        self.assertIn("OPEN (disabled)", digest)


if __name__ == "__main__":
    unittest.main()
