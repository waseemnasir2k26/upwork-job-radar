"""upwork-job-radar scoring engine.

Read-only by design: nothing in this package submits a proposal, sends a
message, or spends a Connect. It scores jobs someone else fetched and renders
alerts for a human to act on.
"""

from .caps import DEFAULT_CAPS, CapExceeded, CapsGuard, CircuitOpen
from .score import DEFAULT_RUBRIC, score_job, score_jobs
from .slack import build_slack_blocks, draft_opener, render_digest, render_text_alert

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_RUBRIC",
    "DEFAULT_CAPS",
    "CapsGuard",
    "CapExceeded",
    "CircuitOpen",
    "score_job",
    "score_jobs",
    "render_text_alert",
    "build_slack_blocks",
    "draft_opener",
    "render_digest",
    "__version__",
]
