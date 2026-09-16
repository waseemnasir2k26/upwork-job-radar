# Changelog

All notable changes to this project are documented in this file.

## [2026.09] - 2026-09-16

- Maintenance review of `upwork-job-radar` — a read-only Upwork job scorer: it polls the official Upwork API on a schedule, scores each new job against a rubric you control, and alerts a human in Slack with the full per-criterion reasoning. It never applies and never bids.
- Stack: pure-stdlib Python 3.9+ (`scoring/` = score, caps, config, slack, cli) with PyYAML as an optional extra for the YAML config, plus a mirrored JavaScript implementation in `workflows/code-nodes/` and an importable n8n workflow at `workflows/upwork-job-radar.json`. MIT licensed, zero runtime dependencies.
- Status: public repo at v1.0.0 (`pyproject.toml`), first released 2026-09-01. Ships example config (`config/rubric.example.yaml`, `config/caps.example.yaml`), `.env.example`, sample job fixtures, and a test suite covering scoring, caps, CLI, Slack blocks, the workflow build and a Python↔JavaScript parity check.
- Reviewed September 2026: documentation refreshed and tagged v2026.09. Rubric, caps, scorer and workflow JSON left untouched; the Python package version in `pyproject.toml` is deliberately unchanged at 1.0.0 since this release carries no code change.
- Known gaps visible in the repo: no CHANGELOG before this release, no CI workflow under `.github/` to run the existing tests, and `[project.urls] Homepage` in `pyproject.toml` still points at `github.com/skynetlabs/upwork-job-radar` rather than this repository's actual location.
