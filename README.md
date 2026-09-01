# upwork-job-radar

Polls the official Upwork API on a schedule, scores every new job against a rubric you control, and posts only the ones worth your time into Slack — with the full reasoning attached. It never applies to anything.

```
UPWORK JOB RADAR  |  score 89/100  (alert at 70+)
--------------------------------------------------------------
Ongoing n8n and workflow automation retainer
$6,000 fixed  |  SG  |  14 proposals
client: 58% hire rate  |  $33,000 spent  |  payment verified

why it scored this
  client_country   ##########  10.0/10    SG allowed
  hire_rate        #########.  13.5/15    58% hire rate
  client_spend     ##########  12.0/12    $33,000 lifetime spend
  payment_verified ##########   8.0/8     verified
  budget           ##########  20.0/20    $6,000 fixed
  proposals        ########..  12.0/15    14 proposals so far
  keywords         ##########  12.0/12    4 bonus term(s): n8n, automation, workflow
  freshness        ##........   1.4/8     posted 40.0h ago

open on Upwork: https://www.upwork.com/jobs/~example010
(read-only alert - nothing was sent to Upwork, apply manually)

draft opener (not sent):
  Hi - on "Ongoing n8n and workflow automation retainer": the part I would
  start with is n8n, Automation, API Integration, because that is where this
  scope usually stalls.
  Two questions before I quote: what does done look like for you, and who
  owns the accounts/credentials?
  Signal I picked up from the post: SG allowed.
```

That block is real output — it is what `python -m scoring.cli` prints, and the Slack version carries the same numbers in Block Kit.

![license MIT](https://img.shields.io/badge/license-MIT-green.svg)
![python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![n8n workflow](https://img.shields.io/badge/n8n-importable%20workflow-ff6d5a.svg)
![dependencies none](https://img.shields.io/badge/runtime%20deps-none-lightgrey.svg)

## Quick start

You can see the whole thing working before you touch a credential.

```bash
# 1. clone it
git clone https://github.com/YOUR-USERNAME/upwork-job-radar.git
cd upwork-job-radar

# 2. score the bundled sample jobs with the default rubric (no config, no keys)
python -m scoring.cli --jobs tests/fixtures/jobs.sample.json

# 3. run the tests, including the Python <-> JavaScript parity check
python -m unittest discover -s tests -t . -v

# 4. tune the rubric to your own work
cp config/rubric.example.yaml config/rubric.yaml
cp config/caps.example.yaml   config/caps.yaml
python -m scoring.cli --jobs tests/fixtures/jobs.sample.json --rubric config/rubric.yaml --all

# 5. wire it up for real: import workflows/upwork-job-radar.json into n8n,
#    fill in the two credentials and the env vars in .env.example
```

Steps 1–4 need nothing but Python. Step 3's parity check also uses Node if you have it. Step 5 is the only one that needs an Upwork OAuth2 app and a Slack bot token.

## The safety model

Three rules. They are the reason this repo exists, and none of them is a limitation to be lifted later.

**1. Read and alert only. It never applies to anything.**

No auto-apply, no auto-bid, no auto-messaging. Upwork's Terms of Service forbid automated proposal submission, and beyond the rules it is simply a bad idea: automated bidding burns Connects on jobs you would have rejected in two seconds, and it puts your name on words you did not write. The radar's job ends at "here is a job worth your attention, and here is why". A human reads the alert, clicks through, and applies by hand.

The workflow drafts a proposal _opener_ for high scorers. It is a draft. It goes to Slack, labelled as a draft, for you to edit — it never goes to Upwork. `tests/test_workflow.py` asserts there is exactly one HTTP node in the whole workflow, that it is a GraphQL query, and that no node contains a write-shaped call. A second HTTP node is how a read-only tool stops being one, so the test fails the build if one appears.

**2. Official API only. Never browser automation.**

Every fetch goes through the official Upwork GraphQL API over OAuth2. This repo does not scrape upwork.com, does not drive a headless browser, and never will. Browser-automating Upwork is a genuine account-ban vector — you are up against Cloudflare and against a ToS that is explicit about it — and losing a freelancing account to save an API integration is a terrible trade. There is a test that greps the workflow for `puppeteer`, `playwright`, `selenium`, `webdriver` and `headless` and fails if any of them show up.

**3. The caps are a shipped feature, not a promise.**

Any loop that runs unattended every ten minutes needs a way to stop. This one has three caps and a circuit breaker, all read from config files and environment variables — never written into a prompt, never left to a model's judgement. `scoring/caps.py` enforces them and `tests/test_caps.py` proves each one with an injected clock. See the next-but-one section for the numbers.

## Scoring rubric

Eight criteria, 100 points, one alert threshold. Everything below is a default you can change in `config/rubric.yaml`; every alert shows the per-criterion contribution so you can see which knob to turn.

| Criterion          | Weight | Full credit at              | Hard gate (veto)                        |
| ------------------ | ------ | --------------------------- | --------------------------------------- |
| `budget`           | 20     | $3,000 fixed / $75 per hour | below $500 fixed or $35/hr              |
| `hire_rate`        | 15     | 60% and up                  | below 40%                               |
| `proposals`        | 15     | 5 or fewer so far           | —                                       |
| `client_spend`     | 12     | $10,000 lifetime            | off by default                          |
| `keywords`         | 12     | 3+ bonus terms matched      | any `must_not_have` term                |
| `client_country`   | 10     | on the allowlist            | not on the allowlist / on the blocklist |
| `payment_verified` | 8      | verified                    | not verified                            |
| `freshness`        | 8      | posted under 2 hours ago    | —                                       |

Notes that matter when you tune it:

- **Alert threshold defaults to 70.** The point is a channel quiet enough that you read it.
- **A hard gate sets the score to 0** and names itself in the alert. `raw_score` still reports what the job _would_ have scored, which is how you find out a gate is too tight.
- **Weight 0 switches a criterion off entirely**, hard gate included.
- **Unknown clients get half credit by default** (`unknown_client_policy: neutral`). A first-time poster is unknown, not bad. Set it to `penalize` or `disqualify` if you disagree.
- **Freshness decays linearly** from full credit at 2 hours to zero at 48. Job age is the one signal that only moves against you.

## Caps and circuit breaker

| Setting                              | Default | What it does                                                                                                                                                          |
| ------------------------------------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `max_steps_per_run`                  | 200     | Most units of work per execution. One step = one job scored, so a paginated API cannot hand you 5,000 rows and get them all processed.                                |
| `wall_clock_seconds`                 | 120     | Hard stop for one execution. The guard refuses the next step once it elapses. Keep it under n8n's `executionTimeout` (300s here).                                     |
| `spend.cap_usd`                      | 5.00    | Rolling API/LLM spend cap. Persisted between runs, so a crash-loop cannot reset it and start spending again.                                                          |
| `spend.window_hours`                 | 24      | The window the spend counter rolls over on.                                                                                                                           |
| `circuit_breaker.consecutive_errors` | 3       | Consecutive failed runs before the workflow disables itself and alerts Slack. One success resets the count.                                                           |
| `circuit_breaker.cooldown_minutes`   | 60      | After this, the breaker allows exactly one half-open probe run. Probe fails, it re-trips immediately. Probe succeeds, the circuit closes. `0` requires a human reset. |
| `circuit_breaker.auto_disable`       | `true`  | Set `false` to count and alert without ever auto-disabling.                                                                                                           |

A default install spends **$0.00** — the proposal opener is a template, not a model call. The spend cap is there because the moment you drop an LLM node into "Format Slack Alert", this becomes a thing that can quietly bill you every ten minutes.

State lives in n8n's workflow static data (`spend_usd`, `window_start`, `consecutive_errors`, `disabled`, `disabled_at`), so it survives restarts. To reset a tripped breaker by hand, clear the `caps` key in the workflow's static data.

## Why not just use Upwork's own alerts

Upwork's saved searches and email/mobile alerts are genuinely good at one thing: telling you fast that a job matching some keywords exists. They are free, they need no setup, and if that is all you need, use them and skip this repo.

Where they run out:

|                        | Upwork saved-search alerts                               | upwork-job-radar                                                                         |
| ---------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Filtering              | Keywords, category, budget range, a few client filters   | Any rule you can express, weighted and combined into one number                          |
| Client quality         | Some filters exist, but you still open each job to judge | Hire rate, lifetime spend, payment verification and country are scored before you see it |
| Why did this reach me? | Not shown                                                | Per-criterion breakdown on every alert                                                   |
| Tuning                 | Adjust filters, hope                                     | Re-score the same jobs offline against a new rubric and diff the results                 |
| Where it lands         | Email and the mobile app                                 | Your team's Slack channel, deduped, plus a daily digest                                  |
| Noise                  | High, because keyword matching is all it has             | You set the threshold; nothing below it is ever shown                                    |
| Cost / effort          | Free, instant                                            | You run an n8n instance and an Upwork OAuth app                                          |
| Applying               | Manual                                                   | Also manual, deliberately                                                                |

Honest summary: this is worth setting up if you bid often enough that triage is a real cost, and if you have opinions about client quality that a keyword filter cannot express. If you check Upwork twice a week, the built-in alerts are the right tool.

## Repository layout

```
scoring/            standalone, importable, unit-tested Python scorer
  score.py            the rubric engine
  caps.py             the three caps + circuit breaker
  slack.py            alert / digest rendering + the draft opener
  config.py           YAML/JSON config loading
  cli.py              python -m scoring.cli
workflows/
  upwork-job-radar.json   importable n8n workflow (built, not hand-edited)
  code-nodes/*.js         the JS twins that get embedded into the Code nodes
  README.md               node-by-node walkthrough + required credentials
config/             rubric.example.yaml, caps.example.yaml
scripts/            build_workflow.py (regenerates the workflow JSON)
tests/              unit + parity + workflow-structure tests, and fixtures
```

The n8n Code nodes are not hand-written inside the JSON blob. `scripts/build_workflow.py` assembles them from `workflows/code-nodes/*.js`, and `tests/test_workflow.py` fails if the committed JSON drifts from those sources. `tests/test_parity.py` then runs the JavaScript and the Python over the same inputs and compares every number and reason string, so the engine you can read and test in Python is provably the engine n8n executes.

## Contributing

Issues and pull requests are welcome. Two things will get a PR rejected on sight:

1. **Anything that submits to Upwork.** Auto-apply, auto-bid, auto-message, "just a draft that sends itself". This is not a feature request, it is a different project.
2. **Anything that scrapes or browser-automates Upwork.** See rule 2 above.

Everything else — new criteria, better field mapping, other notification targets, a nicer digest — is fair game. If you change scoring logic, change `scoring/score.py` and `workflows/code-nodes/score.js` together and run:

```bash
python scripts/build_workflow.py          # regenerate the workflow JSON
python -m unittest discover -s tests -t . # parity + structure tests must pass
```

The test suite needs no third-party packages. Node is optional but without it the parity tests skip, and a skipped parity test is a known gap rather than a green light.

---

Built by SkynetLabs (Waseem Nasir), who builds automation like this for clients — [skynetjoe.com](https://skynetjoe.com) · hire on [Fiverr](https://fiverr.com/agencies/skynetjoellc) · [book a call](https://calendly.com/skynetlabs/schedule-a-free-consultation).

## License

MIT — see [LICENSE](LICENSE).
