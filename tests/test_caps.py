"""Caps and circuit-breaker tests.

The clock is injected, so time-based behaviour is asserted exactly and the
suite still finishes in milliseconds - no sleeps anywhere.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scoring.caps import DEFAULT_CAPS, CapExceeded, CapsGuard, CircuitOpen

START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A clock you can shove forward."""

    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)
        return self.now


def guard(caps=None, state=None, clock=None):
    clock = clock or FakeClock()
    return CapsGuard(caps=caps, state=state, now_fn=clock), clock


class TestStepCap(unittest.TestCase):

    def test_steps_are_allowed_up_to_the_cap(self):
        g, _ = guard({"max_steps_per_run": 3})
        g.start_run()
        for _ in range(3):
            g.check()
            g.record_step()
        self.assertEqual(g.steps, 3)

    def test_step_cap_raises_on_the_next_check(self):
        g, _ = guard({"max_steps_per_run": 3})
        g.start_run()
        for _ in range(3):
            g.check()
            g.record_step()
        with self.assertRaises(CapExceeded) as ctx:
            g.check()
        self.assertEqual(ctx.exception.cap, "max_steps_per_run")
        self.assertIn("3/3", str(ctx.exception))

    def test_step_counter_resets_each_run_but_total_does_not(self):
        g, _ = guard({"max_steps_per_run": 5})
        g.start_run()
        g.check(); g.record_step()
        g.start_run()
        self.assertEqual(g.steps, 0)
        self.assertEqual(g.total_steps, 1)
        self.assertEqual(g.total_runs, 2)


class TestWallClockCap(unittest.TestCase):

    def test_within_the_window_is_fine(self):
        g, clock = guard({"wall_clock_seconds": 60})
        g.start_run()
        clock.advance(seconds=59)
        g.check()  # must not raise

    def test_wall_clock_cap_raises(self):
        g, clock = guard({"wall_clock_seconds": 60})
        g.start_run()
        clock.advance(seconds=61)
        with self.assertRaises(CapExceeded) as ctx:
            g.check()
        self.assertEqual(ctx.exception.cap, "wall_clock_seconds")

    def test_wall_clock_of_zero_disables_that_cap(self):
        g, clock = guard({"wall_clock_seconds": 0})
        g.start_run()
        clock.advance(days=7)
        g.check()  # must not raise


class TestSpendCap(unittest.TestCase):

    def test_spend_accumulates_across_steps(self):
        g, _ = guard({"spend": {"cap_usd": 1.0}})
        g.start_run()
        for _ in range(4):
            g.check(0.2)
            g.record_step(0.2)
        self.assertAlmostEqual(g.spend_usd, 0.8, places=6)

    def test_projected_cost_that_would_breach_is_refused_before_spending(self):
        g, _ = guard({"spend": {"cap_usd": 1.0}})
        g.start_run()
        g.check(0.9); g.record_step(0.9)
        with self.assertRaises(CapExceeded) as ctx:
            g.check(0.2)
        self.assertEqual(ctx.exception.cap, "spend")
        self.assertAlmostEqual(g.spend_usd, 0.9, places=6)

    def test_spend_survives_a_restart_via_state(self):
        g1, clock = guard({"spend": {"cap_usd": 1.0}})
        g1.start_run()
        g1.check(0.6); g1.record_step(0.6)
        g2 = CapsGuard({"spend": {"cap_usd": 1.0}}, g1.to_dict(), now_fn=clock)
        g2.start_run()
        with self.assertRaises(CapExceeded):
            g2.check(0.5)

    def test_spend_window_rolls_over(self):
        g, clock = guard({"spend": {"cap_usd": 1.0, "window_hours": 24}})
        g.start_run()
        g.check(0.9); g.record_step(0.9)
        clock.advance(hours=25)
        g.start_run()
        self.assertEqual(g.spend_usd, 0.0)
        g.check(0.9)  # a fresh window has room again

    def test_window_does_not_roll_early(self):
        g, clock = guard({"spend": {"cap_usd": 1.0, "window_hours": 24}})
        g.start_run()
        g.check(0.9); g.record_step(0.9)
        clock.advance(hours=23)
        g.start_run()
        self.assertAlmostEqual(g.spend_usd, 0.9, places=6)


class TestCircuitBreaker(unittest.TestCase):

    CAPS = {"circuit_breaker": {"consecutive_errors": 3, "cooldown_minutes": 60,
                                "auto_disable": True}}

    def test_errors_below_the_limit_do_not_trip_it(self):
        g, _ = guard(self.CAPS)
        self.assertFalse(g.record_error("boom"))
        self.assertFalse(g.record_error("boom"))
        self.assertFalse(g.disabled)

    def test_nth_consecutive_error_trips_it(self):
        g, _ = guard(self.CAPS)
        g.record_error("boom"); g.record_error("boom")
        self.assertTrue(g.record_error("upstream 500"))
        self.assertTrue(g.disabled)
        self.assertIn("3 consecutive errors", g.disabled_reason)
        self.assertIn("upstream 500", g.disabled_reason)

    def test_a_disabled_guard_refuses_to_start(self):
        g, _ = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        with self.assertRaises(CircuitOpen):
            g.start_run()

    def test_success_clears_the_streak(self):
        g, _ = guard(self.CAPS)
        g.record_error("boom"); g.record_error("boom")
        g.record_success()
        self.assertEqual(g.consecutive_errors, 0)
        self.assertFalse(g.record_error("boom"))
        self.assertFalse(g.disabled)

    def test_cooldown_allows_one_half_open_probe(self):
        g, clock = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        clock.advance(minutes=61)
        g.start_run()  # half-open probe
        self.assertTrue(g.half_open)
        self.assertFalse(g.disabled)

    def test_a_failed_probe_re_trips_immediately(self):
        g, clock = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        clock.advance(minutes=61)
        g.start_run()
        self.assertTrue(g.record_error("still broken"))
        self.assertTrue(g.disabled)
        self.assertIn("half-open probe failed", g.disabled_reason)

    def test_a_successful_probe_closes_the_circuit(self):
        g, clock = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        clock.advance(minutes=61)
        g.start_run()
        g.record_success()
        self.assertFalse(g.half_open)
        self.assertFalse(g.disabled)
        g.start_run()  # normal run, no exception

    def test_cooldown_not_elapsed_stays_disabled(self):
        g, clock = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        clock.advance(minutes=59)
        with self.assertRaises(CircuitOpen):
            g.start_run()

    def test_auto_disable_off_never_trips(self):
        g, _ = guard({"circuit_breaker": {"consecutive_errors": 2,
                                          "auto_disable": False}})
        for _ in range(10):
            self.assertFalse(g.record_error("boom"))
        self.assertFalse(g.disabled)
        self.assertEqual(g.consecutive_errors, 10)

    def test_manual_reset_closes_the_circuit(self):
        g, _ = guard(self.CAPS)
        for _ in range(3):
            g.record_error("boom")
        g.reset()
        self.assertFalse(g.disabled)
        self.assertEqual(g.consecutive_errors, 0)
        g.start_run()

    def test_check_also_refuses_while_disabled(self):
        g, _ = guard(self.CAPS)
        g.start_run()
        for _ in range(3):
            g.record_error("boom")
        with self.assertRaises(CircuitOpen):
            g.check()


class TestPersistence(unittest.TestCase):

    def test_state_round_trips_through_json(self):
        import json
        g, clock = guard()
        g.start_run()
        g.check(0.01); g.record_step(0.01)
        g.record_error("boom")
        state = json.loads(json.dumps(g.to_dict()))
        restored = CapsGuard.from_dict(None, state, now_fn=clock)
        self.assertEqual(restored.to_dict(), g.to_dict())

    def test_defaults_are_used_when_no_caps_are_given(self):
        g, _ = guard()
        self.assertEqual(g.caps["max_steps_per_run"],
                         DEFAULT_CAPS["max_steps_per_run"])
        self.assertEqual(g.caps["spend"]["cap_usd"],
                         DEFAULT_CAPS["spend"]["cap_usd"])

    def test_partial_caps_only_override_what_they_name(self):
        g, _ = guard({"spend": {"cap_usd": 0.25}})
        self.assertEqual(g.caps["spend"]["cap_usd"], 0.25)
        self.assertEqual(g.caps["spend"]["window_hours"], 24)
        self.assertEqual(g.caps["max_steps_per_run"], 200)

    def test_remaining_reports_headroom(self):
        g, clock = guard({"max_steps_per_run": 10, "wall_clock_seconds": 60,
                          "spend": {"cap_usd": 2.0}})
        g.start_run()
        g.check(0.5); g.record_step(0.5)
        clock.advance(seconds=5)
        report = g.remaining()
        self.assertEqual(report["steps_used"], 1)
        self.assertEqual(report["steps_cap"], 10)
        self.assertEqual(report["seconds_used"], 5.0)
        self.assertEqual(report["spend_used_usd"], 0.5)
        self.assertFalse(report["disabled"])


if __name__ == "__main__":
    unittest.main()
