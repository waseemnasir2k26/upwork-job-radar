"""Loop safety caps and circuit breaker.

Three caps, all read from config (``config/caps.example.yaml``), none of them
hardcoded in a prompt:

1. ``max_steps_per_run``   - how many units of work one execution may do
2. ``wall_clock_seconds``  - how long one execution may run
3. ``spend.cap_usd``       - rolling API/LLM spend inside ``spend.window_hours``

Plus a circuit breaker: N consecutive errors disables the loop, alerts, and
stays disabled until a cooldown passes (then one half-open probe run) or a
human resets it.

State is a plain JSON-serialisable dict so it can live in n8n's
``$getWorkflowStaticData('global')`` or in a file - see ``to_dict`` /
``from_dict``. The clock is injectable, which is why the tests are fast and
deterministic instead of full of sleeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

__all__ = [
    "DEFAULT_CAPS",
    "CapExceeded",
    "CircuitOpen",
    "CapsGuard",
]

DEFAULT_CAPS: Dict[str, Any] = {
    "max_steps_per_run": 200,
    "wall_clock_seconds": 120,
    "spend": {
        "cap_usd": 5.0,
        "window_hours": 24,
    },
    "circuit_breaker": {
        "consecutive_errors": 3,
        "cooldown_minutes": 60,
        "auto_disable": True,
    },
}


class CapExceeded(RuntimeError):
    """A configured cap was hit. ``cap`` names which one."""

    def __init__(self, cap: str, message: str):
        super().__init__(message)
        self.cap = cap
        self.message = message


class CircuitOpen(CapExceeded):
    """The circuit breaker is open - the loop is disabled."""

    def __init__(self, message: str):
        super().__init__("circuit_breaker", message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Serialise as `2026-09-01T12:00:00.000Z`.

    Exactly what JavaScript's Date.toISOString() produces, so the state this
    module writes and the state the n8n Code node writes are interchangeable
    (and the parity test can compare them byte for byte).
    """
    if value is None:
        return None
    return (value.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


class CapsGuard:
    """Enforces the three caps plus the circuit breaker.

    Usage::

        guard = CapsGuard(caps, state=load_state())
        guard.start_run()                 # raises CircuitOpen if disabled
        for job in jobs:
            guard.check()                 # raises CapExceeded on steps/time/spend
            guard.record_step(cost_usd=0.0004)
        guard.record_success()
        save_state(guard.to_dict())
    """

    def __init__(self,
                 caps: Optional[Dict[str, Any]] = None,
                 state: Optional[Dict[str, Any]] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.caps = _merge(DEFAULT_CAPS, caps)
        self.now_fn = now_fn or _utcnow
        state = dict(state or {})
        self.spend_usd = float(state.get("spend_usd", 0.0) or 0.0)
        self.window_start = _parse(state.get("window_start")) or self.now_fn()
        self.consecutive_errors = int(state.get("consecutive_errors", 0) or 0)
        self.disabled = bool(state.get("disabled", False))
        self.disabled_reason = state.get("disabled_reason")
        self.disabled_at = _parse(state.get("disabled_at"))
        self.last_error = state.get("last_error")
        self.total_runs = int(state.get("total_runs", 0) or 0)
        self.total_steps = int(state.get("total_steps", 0) or 0)
        self.half_open = bool(state.get("half_open", False))
        self.steps = 0
        self.run_started: Optional[datetime] = None

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spend_usd": round(self.spend_usd, 6),
            "window_start": _iso(self.window_start),
            "consecutive_errors": self.consecutive_errors,
            "disabled": self.disabled,
            "disabled_reason": self.disabled_reason,
            "disabled_at": _iso(self.disabled_at),
            "last_error": self.last_error,
            "total_runs": self.total_runs,
            "total_steps": self.total_steps,
            "half_open": self.half_open,
        }

    @classmethod
    def from_dict(cls, caps: Optional[Dict[str, Any]],
                  state: Optional[Dict[str, Any]],
                  now_fn: Optional[Callable[[], datetime]] = None) -> "CapsGuard":
        return cls(caps=caps, state=state, now_fn=now_fn)

    # -- internals -------------------------------------------------------

    def _roll_spend_window(self) -> None:
        window_hours = float(self.caps["spend"].get("window_hours", 24))
        if self.now_fn() - self.window_start >= timedelta(hours=window_hours):
            self.spend_usd = 0.0
            self.window_start = self.now_fn()

    def _cooldown_elapsed(self) -> bool:
        minutes = float(self.caps["circuit_breaker"].get("cooldown_minutes", 0))
        if self.disabled_at is None or minutes <= 0:
            return False
        return self.now_fn() - self.disabled_at >= timedelta(minutes=minutes)

    def _trip(self, reason: str) -> None:
        self.disabled = True
        self.disabled_reason = reason
        self.disabled_at = self.now_fn()
        self.half_open = False

    # -- run lifecycle ---------------------------------------------------

    def start_run(self) -> None:
        """Begin an execution. Raises CircuitOpen if the breaker is open."""
        if self.disabled:
            if self._cooldown_elapsed():
                # half-open: allow exactly one probe run. One error re-trips it.
                self.disabled = False
                self.half_open = True
            else:
                raise CircuitOpen(
                    "circuit breaker open: " + str(self.disabled_reason))
        self._roll_spend_window()
        self.steps = 0
        self.run_started = self.now_fn()
        self.total_runs += 1

    def check(self, projected_cost_usd: float = 0.0) -> None:
        """Raise CapExceeded if the next step would breach any cap."""
        if self.disabled:
            raise CircuitOpen("circuit breaker open: " + str(self.disabled_reason))

        max_steps = int(self.caps.get("max_steps_per_run", 0) or 0)
        if max_steps and self.steps >= max_steps:
            raise CapExceeded(
                "max_steps_per_run",
                "step cap reached: " + str(self.steps) + "/" + str(max_steps))

        wall = float(self.caps.get("wall_clock_seconds", 0) or 0)
        if wall and self.run_started is not None:
            elapsed = (self.now_fn() - self.run_started).total_seconds()
            if elapsed >= wall:
                raise CapExceeded(
                    "wall_clock_seconds",
                    "wall clock cap reached: {:.1f}s/{:.0f}s".format(elapsed, wall))

        cap_usd = float(self.caps["spend"].get("cap_usd", 0) or 0)
        if cap_usd:
            self._roll_spend_window()
            if self.spend_usd + projected_cost_usd > cap_usd:
                raise CapExceeded(
                    "spend",
                    "spend cap reached: ${:.4f}/${:.2f} in the last {}h".format(
                        self.spend_usd + projected_cost_usd, cap_usd,
                        self.caps["spend"].get("window_hours", 24)))

    def record_step(self, cost_usd: float = 0.0) -> None:
        """Count one unit of work and its cost. Call AFTER check()."""
        self.steps += 1
        self.total_steps += 1
        if cost_usd:
            self._roll_spend_window()
            self.spend_usd += float(cost_usd)

    def record_error(self, message: str = "") -> bool:
        """Record a failed run. Returns True if this tripped the breaker."""
        self.consecutive_errors += 1
        self.last_error = message or None
        breaker = self.caps["circuit_breaker"]
        if not breaker.get("auto_disable", True):
            return False
        limit = int(breaker.get("consecutive_errors", 3) or 3)
        if self.half_open:
            # A probe run that fails re-opens the circuit immediately.
            self._trip("half-open probe failed: " + (message or "unknown error"))
            return True
        if self.consecutive_errors >= limit:
            self._trip(str(self.consecutive_errors) +
                       " consecutive errors (limit " + str(limit) + "): " +
                       (message or "unknown error"))
            return True
        return False

    def record_success(self) -> None:
        """Record a clean run: clears the error streak and any half-open flag."""
        self.consecutive_errors = 0
        self.last_error = None
        self.half_open = False

    def reset(self) -> None:
        """Manual human reset: closes the breaker, clears the error streak."""
        self.disabled = False
        self.disabled_reason = None
        self.disabled_at = None
        self.consecutive_errors = 0
        self.half_open = False

    # -- reporting -------------------------------------------------------

    def remaining(self) -> Dict[str, Any]:
        """Human-readable headroom, used by the Slack digest."""
        self._roll_spend_window()
        max_steps = int(self.caps.get("max_steps_per_run", 0) or 0)
        wall = float(self.caps.get("wall_clock_seconds", 0) or 0)
        elapsed = 0.0
        if self.run_started is not None:
            elapsed = (self.now_fn() - self.run_started).total_seconds()
        cap_usd = float(self.caps["spend"].get("cap_usd", 0) or 0)
        return {
            "steps_used": self.steps,
            "steps_cap": max_steps,
            "seconds_used": round(elapsed, 2),
            "seconds_cap": wall,
            "spend_used_usd": round(self.spend_usd, 4),
            "spend_cap_usd": cap_usd,
            "spend_window_hours": self.caps["spend"].get("window_hours", 24),
            "consecutive_errors": self.consecutive_errors,
            "disabled": self.disabled,
            "disabled_reason": self.disabled_reason,
            "half_open": self.half_open,
        }
