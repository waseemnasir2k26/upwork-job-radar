"""Slack message rendering + the proposal-opener draft.

Nothing here talks to Upwork. ``draft_opener`` produces a *draft* the human
copies, edits and sends by hand. It is a deterministic template, not an LLM
call, so the default install spends nothing and the output is unit-testable.
Swap in an LLM node in n8n if you want richer drafts - the spend cap in
``caps.py`` exists precisely because that node costs money.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["render_text_alert", "build_slack_blocks", "draft_opener",
           "render_digest"]

_BAR_WIDTH = 10


def _bar(earned: float, maximum: float) -> str:
    if maximum <= 0:
        return " " * _BAR_WIDTH
    filled = int(round(_BAR_WIDTH * max(0.0, min(1.0, earned / maximum))))
    return "#" * filled + "." * (_BAR_WIDTH - filled)


def _money(value: Any) -> str:
    try:
        return "${:,.0f}".format(float(value))
    except (TypeError, ValueError):
        return "n/a"


def _budget_line(job: Dict[str, Any]) -> str:
    if str(job.get("job_type") or "fixed").lower() == "hourly":
        low = job.get("hourly_min")
        high = job.get("hourly_max")
        if low and high:
            return _money(low) + "-" + _money(high) + "/hr"
        if high or low:
            return _money(high or low) + "/hr"
        return "hourly, rate not stated"
    if job.get("budget"):
        return _money(job.get("budget")) + " fixed"
    return "budget not stated"


def render_text_alert(result: Dict[str, Any], job: Dict[str, Any]) -> str:
    """Plain-text alert. This is exactly what the README mock shows."""
    client = job.get("client") or {}
    hire = client.get("hire_rate")
    hire_txt = "n/a"
    if hire is not None:
        rate = float(hire)
        rate = rate / 100.0 if rate > 1 else rate
        hire_txt = "{:.0f}%".format(rate * 100)

    lines: List[str] = []
    lines.append("UPWORK JOB RADAR  |  score {}/100  (alert at {}+)".format(
        result.get("score"), result.get("threshold")))
    lines.append("-" * 62)
    lines.append(str(job.get("title") or "(untitled)"))
    lines.append("{}  |  {}  |  {} proposals".format(
        _budget_line(job),
        str(client.get("country") or "??"),
        job.get("proposals") if job.get("proposals") is not None else "?"))
    lines.append("client: {} hire rate  |  {} spent  |  payment {}".format(
        hire_txt,
        _money(client.get("total_spend")),
        "verified" if client.get("payment_verified") else "NOT verified"))
    lines.append("")
    lines.append("why it scored this")
    for item in result.get("breakdown") or []:
        lines.append("  {:<16} {} {:>5.1f}/{:<4.0f}  {}".format(
            item["key"], _bar(item["earned"], item["maximum"]),
            item["earned"], item["maximum"], item["reason"]))
    if result.get("disqualifiers"):
        lines.append("")
        lines.append("DISQUALIFIED:")
        for reason in result["disqualifiers"]:
            lines.append("  x " + reason)
    lines.append("")
    lines.append("open on Upwork: " + str(job.get("url") or "n/a"))
    lines.append("(read-only alert - nothing was sent to Upwork, apply manually)")
    return "\n".join(lines)


def build_slack_blocks(result: Dict[str, Any],
                       job: Dict[str, Any],
                       opener: Optional[str] = None) -> Dict[str, Any]:
    """Slack chat.postMessage payload body (blocks + fallback text)."""
    client = job.get("client") or {}
    breakdown_lines = [
        "`{}` {} {}/{} - {}".format(
            item["key"], _bar(item["earned"], item["maximum"]),
            item["earned"], int(item["maximum"]), item["reason"])
        for item in (result.get("breakdown") or [])
    ]
    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Score {} - {}".format(
                    result.get("score"),
                    str(job.get("title") or "(untitled)")[:120]),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Budget*\n" + _budget_line(job)},
                {"type": "mrkdwn", "text": "*Country*\n" +
                 str(client.get("country") or "unknown")},
                {"type": "mrkdwn", "text": "*Proposals*\n" +
                 str(job.get("proposals") if job.get("proposals") is not None else "unknown")},
                {"type": "mrkdwn", "text": "*Client spend*\n" +
                 _money(client.get("total_spend"))},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*Why it scored this*\n" + "\n".join(breakdown_lines)},
        },
    ]
    if opener:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*Draft opener (yours to edit - nothing was sent)*\n>"
                             + opener.replace("\n", "\n>")},
        })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "Read-only alert. Nothing was submitted to Upwork. "
                    "<{}|Open the job> and apply by hand.".format(
                        job.get("url") or "https://www.upwork.com"),
        }],
    })
    return {
        "text": "Upwork Job Radar: {} scored {}".format(
            str(job.get("title") or "(untitled)")[:80], result.get("score")),
        "blocks": blocks,
    }


def draft_opener(job: Dict[str, Any], result: Dict[str, Any]) -> str:
    """A short, deterministic proposal opener draft. Never sent anywhere."""
    title = str(job.get("title") or "your project").strip()
    skills = [str(s) for s in (job.get("skills") or [])][:3]
    skills_txt = ", ".join(skills) if skills else "the stack you listed"
    strengths = sorted(
        [b for b in (result.get("breakdown") or []) if b["maximum"] > 0],
        key=lambda b: b["earned"] / b["maximum"], reverse=True)[:2]
    angle = strengths[0]["reason"] if strengths else "the brief"
    return (
        "Hi - on \"{title}\": the part I would start with is {skills}, "
        "because that is where this scope usually stalls.\n"
        "Two questions before I quote: what does done look like for you, and "
        "who owns the accounts/credentials?\n"
        "Signal I picked up from the post: {angle}."
    ).format(title=title, skills=skills_txt, angle=angle)


def render_digest(results: List[Dict[str, Any]],
                  caps_state: Optional[Dict[str, Any]] = None,
                  window_label: str = "last 24h") -> str:
    """Plain-text daily digest."""
    passed = [r for r in results if r.get("passed")]
    disq = [r for r in results if r.get("disqualified")]
    lines = [
        "UPWORK JOB RADAR - daily digest ({})".format(window_label),
        "-" * 62,
        "{} scored | {} passed the threshold | {} disqualified".format(
            len(results), len(passed), len(disq)),
        "",
    ]
    if passed:
        lines.append("passed:")
        for r in passed[:10]:
            lines.append("  {:>3}  {}".format(
                r.get("score"), str(r.get("title") or "")[:56]))
    else:
        lines.append("passed: none. That is a normal day, not a broken radar.")
    if disq:
        lines.append("")
        lines.append("top disqualifier reasons:")
        counts: Dict[str, int] = {}
        for r in disq:
            for reason in r.get("disqualifiers") or []:
                key = reason.split(":")[0]
                counts[key] = counts.get(key, 0) + 1
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
            lines.append("  {:>3}x  {}".format(count, reason))
    if caps_state:
        lines.append("")
        lines.append("caps: {}/{} steps | ${:.4f}/${:.2f} spend | breaker {}".format(
            caps_state.get("steps_used", 0), caps_state.get("steps_cap", 0),
            caps_state.get("spend_used_usd", 0.0),
            caps_state.get("spend_cap_usd", 0.0),
            "OPEN (disabled)" if caps_state.get("disabled") else "closed"))
    return "\n".join(lines)
