"""Standalone Upwork job scorer.

Pure functions, no I/O, no network. The same logic is mirrored in
``workflows/code-nodes/score.js`` so the n8n Code node and this module cannot
drift (``tests/test_parity.py`` fails the build if they do).

The scorer NEVER contacts Upwork and NEVER submits anything. It takes a job
dict that some caller already fetched from the official API and returns a
score plus a per-criterion breakdown a human can read and argue with.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "DEFAULT_RUBRIC",
    "CriterionResult",
    "ScoreResult",
    "score_job",
    "score_jobs",
]

# --------------------------------------------------------------------------
# Defaults. config/rubric.example.yaml is a serialised copy of this dict.
# --------------------------------------------------------------------------

DEFAULT_RUBRIC: Dict[str, Any] = {
    "alert_threshold": 70,
    "unknown_client_policy": "neutral",  # neutral | penalize | disqualify
    "weights": {
        "client_country": 10,
        "hire_rate": 15,
        "client_spend": 12,
        "payment_verified": 8,
        "budget": 20,
        "proposals": 15,
        "keywords": 12,
        "freshness": 8,
    },
    "client_country": {
        "allowlist": [
            "US", "CA", "GB", "AU", "NZ", "IE",
            "DE", "NL", "SE", "NO", "DK", "FI",
            "CH", "AT", "BE", "FR", "SG", "AE",
        ],
        "blocklist": [],
        "hard_gate": True,
    },
    "hire_rate": {
        "target": 0.60,
        "hard_fail_below": 0.40,
        "hard_gate": True,
    },
    "client_spend": {
        "target_usd": 10000,
        "hard_fail_below_usd": 0,
        "hard_gate": False,
    },
    "payment_verified": {
        "required": True,
    },
    "budget": {
        "fixed_floor_usd": 500,
        "fixed_target_usd": 3000,
        "hourly_floor_usd": 35,
        "hourly_target_usd": 75,
        "hard_gate": True,
        "missing_budget_policy": "neutral",  # neutral | penalize | disqualify
    },
    "proposals": {
        "ideal_max": 5,
        "zero_at": 50,
    },
    "keywords": {
        "must_have_any": [],
        "must_not_have": [
            "unpaid",
            "revenue share only",
            "equity only",
            "no budget",
        ],
        "bonus": [
            "n8n", "automation", "workflow", "api integration",
            "wordpress", "shopify", "openai", "anthropic", "webhook",
        ],
        "hard_gate": True,
    },
    "freshness": {
        "fresh_hours": 2,
        "stale_hours": 48,
    },
}


def _round_half_up(value: float, digits: int = 0) -> float:
    """Half-up rounding.

    Python's built-in round() is half-to-even and JavaScript's Math.round is
    half-up. The n8n Code node runs the JS twin of this file, so both sides
    use this one rule or the parity test fails.
    """
    factor = 10 ** digits
    scaled = math.floor(abs(value) * factor + 0.5) / factor
    result = -scaled if value < 0 else scaled
    return result if digits > 0 else float(result)


class CriterionResult(dict):
    """One rubric line item. A dict subclass so it JSON-serialises as-is."""

    def __init__(self, key: str, earned: float, maximum: float, reason: str):
        super().__init__(key=key, earned=_round_half_up(earned, 2),
                         maximum=_round_half_up(maximum, 2), reason=reason)


class ScoreResult(dict):
    """Full result for one job."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Shallow per-section merge, so a partial rubric only overrides what it names."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ramp(value: float, zero_at: float, full_at: float) -> float:
    """Linear 0..1 ramp between zero_at and full_at."""
    if full_at == zero_at:
        return 1.0 if value >= full_at else 0.0
    return _clamp((value - zero_at) / (full_at - zero_at))


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _haystack(job: Dict[str, Any]) -> str:
    parts: List[str] = [
        str(job.get("title") or ""),
        str(job.get("description") or ""),
        " ".join(str(s) for s in (job.get("skills") or [])),
    ]
    return " ".join(parts).lower()


def _pct(value: Any) -> Optional[float]:
    """Accept a hire rate expressed either as 0.55 or as 55."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 100.0 if number > 1 else number


def _money(value: float) -> str:
    return "{:,}".format(int(_round_half_up(value, 0)))


def _pctstr(value: float) -> str:
    return "{:,}%".format(int(_round_half_up(value * 100, 0)))


def _hours(value: float) -> str:
    return "{:.1f}".format(_round_half_up(value, 1))


# --------------------------------------------------------------------------
# criteria
# --------------------------------------------------------------------------

def _c_country(job, rub, weight, disq):
    cfg = rub["client_country"]
    country = (job.get("client") or {}).get("country")
    code = str(country).upper() if country else None
    allow = [str(c).upper() for c in cfg.get("allowlist") or []]
    block = [str(c).upper() for c in cfg.get("blocklist") or []]
    if code is None:
        return CriterionResult("client_country", weight * 0.5, weight,
                               "client country unknown - half credit")
    if code in block:
        if cfg.get("hard_gate"):
            disq.append("client country " + code + " is on the blocklist")
        return CriterionResult("client_country", 0, weight,
                               code + " is blocklisted")
    if allow and code not in allow:
        if cfg.get("hard_gate"):
            disq.append("client country " + code + " is not on the allowlist")
        return CriterionResult("client_country", 0, weight,
                               code + " is not on the allowlist")
    return CriterionResult("client_country", weight, weight, code + " allowed")


def _c_hire_rate(job, rub, weight, disq):
    cfg = rub["hire_rate"]
    rate = _pct((job.get("client") or {}).get("hire_rate"))
    if rate is None:
        policy = rub.get("unknown_client_policy", "neutral")
        if policy == "disqualify":
            disq.append("client has no hire-rate history")
            return CriterionResult("hire_rate", 0, weight, "no hire history")
        share = 0.0 if policy == "penalize" else 0.5
        return CriterionResult("hire_rate", weight * share, weight,
                               "no hire history - policy '" + policy + "'")
    floor = float(cfg.get("hard_fail_below", 0))
    if rate < floor:
        if cfg.get("hard_gate"):
            disq.append("hire rate " + _pctstr(rate) + " is below the " +
                        _pctstr(floor) + " floor")
        return CriterionResult("hire_rate", 0, weight,
                               _pctstr(rate) + " below " + _pctstr(floor) + " floor")
    share = _ramp(rate, floor, float(cfg.get("target", 1.0)))
    return CriterionResult("hire_rate", weight * share, weight,
                           _pctstr(rate) + " hire rate")


def _c_spend(job, rub, weight, disq):
    cfg = rub["client_spend"]
    spend = (job.get("client") or {}).get("total_spend")
    if spend is None:
        policy = rub.get("unknown_client_policy", "neutral")
        if policy == "disqualify":
            disq.append("client spend unknown")
            return CriterionResult("client_spend", 0, weight, "spend unknown")
        share = 0.0 if policy == "penalize" else 0.5
        return CriterionResult("client_spend", weight * share, weight,
                               "spend unknown - policy '" + policy + "'")
    spend = float(spend)
    floor = float(cfg.get("hard_fail_below_usd", 0))
    if spend < floor:
        if cfg.get("hard_gate"):
            disq.append("client spend $" + _money(spend) + " below $" + _money(floor))
        return CriterionResult("client_spend", 0, weight,
                               "$" + _money(spend) + " below floor")
    share = _ramp(spend, floor, float(cfg.get("target_usd", 1)))
    return CriterionResult("client_spend", weight * share, weight,
                           "$" + _money(spend) + " lifetime spend")


def _c_payment(job, rub, weight, disq):
    cfg = rub["payment_verified"]
    verified = (job.get("client") or {}).get("payment_verified")
    if verified is None:
        return CriterionResult("payment_verified", weight * 0.5, weight,
                               "verification status unknown")
    if not verified:
        if cfg.get("required"):
            disq.append("client payment method is not verified")
        return CriterionResult("payment_verified", 0, weight, "not verified")
    return CriterionResult("payment_verified", weight, weight, "verified")


def _c_budget(job, rub, weight, disq):
    cfg = rub["budget"]
    job_type = str(job.get("job_type") or "fixed").lower()
    if job_type == "hourly":
        rate = job.get("hourly_max") or job.get("hourly_min")
        floor = float(cfg.get("hourly_floor_usd", 0))
        target = float(cfg.get("hourly_target_usd", 1))
        label = "/hr"
    else:
        rate = job.get("budget")
        floor = float(cfg.get("fixed_floor_usd", 0))
        target = float(cfg.get("fixed_target_usd", 1))
        label = " fixed"
    if rate is None:
        policy = cfg.get("missing_budget_policy", "neutral")
        if policy == "disqualify":
            disq.append("job has no stated budget")
            return CriterionResult("budget", 0, weight, "no budget stated")
        share = 0.0 if policy == "penalize" else 0.5
        return CriterionResult("budget", weight * share, weight,
                               "no budget stated - policy '" + policy + "'")
    rate = float(rate)
    if rate < floor:
        if cfg.get("hard_gate"):
            disq.append("$" + _money(rate) + label + " is below the $" +
                        _money(floor) + " floor")
        return CriterionResult("budget", 0, weight,
                               "$" + _money(rate) + label + " below floor")
    share = _ramp(rate, floor, target)
    return CriterionResult("budget", weight * share, weight,
                           "$" + _money(rate) + label)


def _c_proposals(job, rub, weight, disq):
    cfg = rub["proposals"]
    count = job.get("proposals")
    if count is None:
        return CriterionResult("proposals", weight * 0.5, weight,
                               "proposal count unknown")
    count = float(count)
    ideal = float(cfg.get("ideal_max", 5))
    zero_at = float(cfg.get("zero_at", 50))
    if count <= ideal:
        share = 1.0
    elif zero_at <= ideal:
        share = 0.0
    else:
        share = _clamp((zero_at - count) / (zero_at - ideal))
    return CriterionResult("proposals", weight * share, weight,
                           str(int(count)) + " proposals so far")


def _c_keywords(job, rub, weight, disq):
    cfg = rub["keywords"]
    text = _haystack(job)
    banned = [k for k in (cfg.get("must_not_have") or []) if str(k).lower() in text]
    if banned:
        if cfg.get("hard_gate"):
            disq.append("banned keyword: " + str(banned[0]))
        return CriterionResult("keywords", 0, weight,
                               "banned keyword '" + str(banned[0]) + "'")
    bonus_hits = [k for k in (cfg.get("bonus") or []) if str(k).lower() in text]
    must = cfg.get("must_have_any") or []
    if must:
        hits = [k for k in must if str(k).lower() in text]
        if not hits:
            if cfg.get("hard_gate"):
                disq.append("no must-have keyword present")
            return CriterionResult("keywords", 0, weight, "no must-have keyword")
        base = weight * 0.7
        extra = weight * 0.3 * _clamp(len(bonus_hits) / 3.0)
        return CriterionResult(
            "keywords", base + extra, weight,
            "must-have '" + str(hits[0]) + "' + " + str(len(bonus_hits)) +
            " bonus term(s)")
    if not bonus_hits:
        return CriterionResult("keywords", 0, weight, "no matching keywords")
    share = _clamp(len(bonus_hits) / 3.0)
    return CriterionResult("keywords", weight * share, weight,
                           str(len(bonus_hits)) + " bonus term(s): " +
                           ", ".join(str(k) for k in bonus_hits[:3]))


def _c_freshness(job, rub, weight, disq, now: datetime):
    cfg = rub["freshness"]
    posted = _parse_ts(job.get("posted_at"))
    if posted is None:
        return CriterionResult("freshness", weight * 0.5, weight,
                               "post time unknown")
    age_hours = (now - posted).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0.0
    fresh = float(cfg.get("fresh_hours", 2))
    stale = float(cfg.get("stale_hours", 48))
    if age_hours <= fresh:
        share = 1.0
    elif stale <= fresh:
        share = 0.0
    else:
        share = _clamp((stale - age_hours) / (stale - fresh))
    return CriterionResult("freshness", weight * share, weight,
                           "posted " + _hours(age_hours) + "h ago")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def score_job(job: Dict[str, Any],
              rubric: Optional[Dict[str, Any]] = None,
              now: Optional[datetime] = None) -> ScoreResult:
    """Score one job dict.

    Returns a ScoreResult with keys: id, title, url, score (0-100 int),
    raw_score, threshold, disqualified, disqualifiers, passed, breakdown.
    A disqualified job always scores 0 - raw_score keeps what it would have
    scored, which is what you want when tuning the rubric.
    """
    rub = _merge(DEFAULT_RUBRIC, rubric)
    now = now or datetime.now(timezone.utc)
    weights = rub["weights"]
    disq: List[str] = []

    handlers = [
        ("client_country", lambda w: _c_country(job, rub, w, disq)),
        ("hire_rate", lambda w: _c_hire_rate(job, rub, w, disq)),
        ("client_spend", lambda w: _c_spend(job, rub, w, disq)),
        ("payment_verified", lambda w: _c_payment(job, rub, w, disq)),
        ("budget", lambda w: _c_budget(job, rub, w, disq)),
        ("proposals", lambda w: _c_proposals(job, rub, w, disq)),
        ("keywords", lambda w: _c_keywords(job, rub, w, disq)),
        ("freshness", lambda w: _c_freshness(job, rub, w, disq, now)),
    ]

    breakdown: List[CriterionResult] = []
    earned = 0.0
    possible = 0.0
    for key, handler in handlers:
        weight = float(weights.get(key, 0) or 0)
        if weight <= 0:
            continue  # weight 0 disables the criterion entirely
        result = handler(weight)
        breakdown.append(result)
        earned += result["earned"]
        possible += result["maximum"]

    raw = 0 if possible <= 0 else int(_round_half_up(earned / possible * 100, 0))
    score = 0 if disq else raw
    threshold = int(rub.get("alert_threshold", 70))

    return ScoreResult(
        id=job.get("id"),
        title=job.get("title"),
        url=job.get("url"),
        score=score,
        raw_score=raw,
        threshold=threshold,
        disqualified=bool(disq),
        disqualifiers=disq,
        passed=(not disq) and score >= threshold,
        breakdown=breakdown,
    )


def score_jobs(jobs: List[Dict[str, Any]],
               rubric: Optional[Dict[str, Any]] = None,
               now: Optional[datetime] = None) -> List[ScoreResult]:
    """Score a list of jobs, highest score first."""
    results = [score_job(job, rubric, now) for job in jobs]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results
