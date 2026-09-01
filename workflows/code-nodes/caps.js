/**
 * JS twin of scoring/caps.py - the three loop caps plus the circuit breaker.
 *
 * Embedded into the "Caps Preflight" and "Persist State" Code nodes of
 * workflows/upwork-job-radar.json by scripts/build_workflow.py. Edit here,
 * then rebuild - never edit the copy inside the workflow JSON.
 *
 * In n8n the state object lives in $getWorkflowStaticData('global').caps, so
 * it survives between executions, which is the whole point of a spend counter.
 *
 * tests/test_parity.py drives the same scripted sequence through both this
 * file and caps.py and fails if the resulting state differs.
 */

const DEFAULT_CAPS = {
  max_steps_per_run: 200,
  wall_clock_seconds: 120,
  spend: { cap_usd: 5.0, window_hours: 24 },
  circuit_breaker: {
    consecutive_errors: 3,
    cooldown_minutes: 60,
    auto_disable: true,
  },
};

class CapExceeded extends Error {
  constructor(cap, message) {
    super(message);
    this.name = "CapExceeded";
    this.cap = cap;
  }
}

class CircuitOpen extends CapExceeded {
  constructor(message) {
    super("circuit_breaker", message);
    this.name = "CircuitOpen";
  }
}

function mergeCaps(base, override) {
  const out = {};
  Object.keys(base).forEach((k) => {
    const v = base[k];
    out[k] =
      v && typeof v === "object" && !Array.isArray(v)
        ? Object.assign({}, v)
        : v;
  });
  if (!override) return out;
  Object.keys(override).forEach((k) => {
    const v = override[k];
    if (
      v &&
      typeof v === "object" &&
      !Array.isArray(v) &&
      out[k] &&
      typeof out[k] === "object" &&
      !Array.isArray(out[k])
    ) {
      out[k] = Object.assign({}, out[k], v);
    } else {
      out[k] = v;
    }
  });
  return out;
}

function parseDate(value) {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  let text = String(value);
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) text += "Z";
  const ms = Date.parse(text);
  return isNaN(ms) ? null : new Date(ms);
}

function isoOf(date) {
  return date === null || date === undefined ? null : date.toISOString();
}

class CapsGuard {
  constructor(caps, state, nowFn) {
    this.caps = mergeCaps(DEFAULT_CAPS, caps);
    this.nowFn = nowFn || (() => new Date());
    const s = state || {};
    this.spendUsd = Number(s.spend_usd || 0);
    this.windowStart = parseDate(s.window_start) || this.nowFn();
    this.consecutiveErrors = Number(s.consecutive_errors || 0);
    this.disabled = Boolean(s.disabled);
    this.disabledReason =
      s.disabled_reason === undefined ? null : s.disabled_reason;
    this.disabledAt = parseDate(s.disabled_at);
    this.lastError = s.last_error === undefined ? null : s.last_error;
    this.totalRuns = Number(s.total_runs || 0);
    this.totalSteps = Number(s.total_steps || 0);
    this.halfOpen = Boolean(s.half_open);
    this.steps = 0;
    this.runStarted = null;
  }

  toDict() {
    return {
      spend_usd: Math.round(this.spendUsd * 1e6) / 1e6,
      window_start: isoOf(this.windowStart),
      consecutive_errors: this.consecutiveErrors,
      disabled: this.disabled,
      disabled_reason: this.disabledReason,
      disabled_at: isoOf(this.disabledAt),
      last_error: this.lastError,
      total_runs: this.totalRuns,
      total_steps: this.totalSteps,
      half_open: this.halfOpen,
    };
  }

  rollSpendWindow() {
    const windowHours = Number(
      this.caps.spend.window_hours === undefined
        ? 24
        : this.caps.spend.window_hours,
    );
    if (
      this.nowFn().getTime() - this.windowStart.getTime() >=
      windowHours * 3600000
    ) {
      this.spendUsd = 0;
      this.windowStart = this.nowFn();
    }
  }

  cooldownElapsed() {
    const minutes = Number(this.caps.circuit_breaker.cooldown_minutes || 0);
    if (!this.disabledAt || minutes <= 0) return false;
    return (
      this.nowFn().getTime() - this.disabledAt.getTime() >= minutes * 60000
    );
  }

  trip(reason) {
    this.disabled = true;
    this.disabledReason = reason;
    this.disabledAt = this.nowFn();
    this.halfOpen = false;
  }

  startRun() {
    if (this.disabled) {
      if (this.cooldownElapsed()) {
        this.disabled = false;
        this.halfOpen = true;
      } else {
        throw new CircuitOpen(
          "circuit breaker open: " + String(this.disabledReason),
        );
      }
    }
    this.rollSpendWindow();
    this.steps = 0;
    this.runStarted = this.nowFn();
    this.totalRuns += 1;
  }

  check(projectedCostUsd) {
    const projected = Number(projectedCostUsd || 0);
    if (this.disabled) {
      throw new CircuitOpen(
        "circuit breaker open: " + String(this.disabledReason),
      );
    }
    const maxSteps = Number(this.caps.max_steps_per_run || 0);
    if (maxSteps && this.steps >= maxSteps) {
      throw new CapExceeded(
        "max_steps_per_run",
        "step cap reached: " + this.steps + "/" + maxSteps,
      );
    }
    const wall = Number(this.caps.wall_clock_seconds || 0);
    if (wall && this.runStarted) {
      const elapsed =
        (this.nowFn().getTime() - this.runStarted.getTime()) / 1000;
      if (elapsed >= wall) {
        throw new CapExceeded(
          "wall_clock_seconds",
          "wall clock cap reached: " +
            elapsed.toFixed(1) +
            "s/" +
            wall.toFixed(0) +
            "s",
        );
      }
    }
    const capUsd = Number(this.caps.spend.cap_usd || 0);
    if (capUsd) {
      this.rollSpendWindow();
      if (this.spendUsd + projected > capUsd) {
        throw new CapExceeded(
          "spend",
          "spend cap reached: $" +
            (this.spendUsd + projected).toFixed(4) +
            "/$" +
            capUsd.toFixed(2) +
            " in the last " +
            this.caps.spend.window_hours +
            "h",
        );
      }
    }
  }

  recordStep(costUsd) {
    this.steps += 1;
    this.totalSteps += 1;
    const cost = Number(costUsd || 0);
    if (cost) {
      this.rollSpendWindow();
      this.spendUsd += cost;
    }
  }

  recordError(message) {
    this.consecutiveErrors += 1;
    this.lastError = message || null;
    const breaker = this.caps.circuit_breaker;
    if (breaker.auto_disable === false) return false;
    const limit = Number(breaker.consecutive_errors || 3);
    if (this.halfOpen) {
      this.trip("half-open probe failed: " + (message || "unknown error"));
      return true;
    }
    if (this.consecutiveErrors >= limit) {
      this.trip(
        this.consecutiveErrors +
          " consecutive errors (limit " +
          limit +
          "): " +
          (message || "unknown error"),
      );
      return true;
    }
    return false;
  }

  recordSuccess() {
    this.consecutiveErrors = 0;
    this.lastError = null;
    this.halfOpen = false;
  }

  reset() {
    this.disabled = false;
    this.disabledReason = null;
    this.disabledAt = null;
    this.consecutiveErrors = 0;
    this.halfOpen = false;
  }

  remaining() {
    this.rollSpendWindow();
    const elapsed = this.runStarted
      ? (this.nowFn().getTime() - this.runStarted.getTime()) / 1000
      : 0;
    return {
      steps_used: this.steps,
      steps_cap: Number(this.caps.max_steps_per_run || 0),
      seconds_used: Math.round(elapsed * 100) / 100,
      seconds_cap: Number(this.caps.wall_clock_seconds || 0),
      spend_used_usd: Math.round(this.spendUsd * 10000) / 10000,
      spend_cap_usd: Number(this.caps.spend.cap_usd || 0),
      spend_window_hours: this.caps.spend.window_hours,
      consecutive_errors: this.consecutiveErrors,
      disabled: this.disabled,
      disabled_reason: this.disabledReason,
      half_open: this.halfOpen,
    };
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { DEFAULT_CAPS, CapsGuard, CapExceeded, CircuitOpen };
}
