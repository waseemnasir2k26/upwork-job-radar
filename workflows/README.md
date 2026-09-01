# The n8n workflow, node by node

`upwork-job-radar.json` imports straight into n8n (Workflows → Import from File). It arrives **inactive**. Read this page before you activate it.

The Code nodes are generated, not hand-written. `scripts/build_workflow.py` assembles them from `code-nodes/*.js`; `tests/test_workflow.py` fails if the committed JSON has drifted. **Edit the `.js` files and rebuild — never edit the JSON.**

```bash
python scripts/build_workflow.py           # rebuild
python scripts/build_workflow.py --check   # exit 1 if stale
```

## Required credentials

Both are created in n8n under **Credentials**, and both node entries in the JSON ship with `"id": "REPLACE_ME"` — n8n will ask you to pick the real credential on first open.

**1. Upwork OAuth2 API** (credential type: _OAuth2 API_, generic)

Register an application at the Upwork developer portal and use its client id / secret. Grant the narrowest read scope your app offers — this workflow only ever issues a search query.

| Field                     | Value                                                         |
| ------------------------- | ------------------------------------------------------------- |
| Grant Type                | Authorization Code                                            |
| Authorization URL         | `https://www.upwork.com/ab/account-security/oauth2/authorize` |
| Access Token URL          | `https://www.upwork.com/api/v3/oauth2/token`                  |
| Client ID / Secret        | from your Upwork app                                          |
| Auth URI Query Parameters | as your app requires                                          |

**2. Slack API** (credential type: _Slack API_, bot token)

A bot token with `chat:write`, invited to the destination channel. The channel id goes in `SLACK_CHANNEL_ID` (see `.env.example`), not in the workflow.

## Configuration

Everything tunable is an environment variable read by the **Load Config** node — nothing is hardcoded in a node and nothing lives in a prompt. Full list with defaults is in `.env.example`. The three caps are `RADAR_MAX_STEPS_PER_RUN`, `RADAR_WALL_CLOCK_SECONDS`, `RADAR_SPEND_CAP_USD` (plus its window), and the breaker is `RADAR_BREAKER_CONSECUTIVE_ERRORS` / `RADAR_BREAKER_COOLDOWN_MINUTES`.

To ship a whole tuned rubric at once, put the JSON form of `config/rubric.yaml` into `RADAR_RUBRIC_JSON`; same for `RADAR_CAPS_JSON`.

## The main path

**1. Every 10 Minutes** — `scheduleTrigger`

The poll interval. 5, 10 and 20 minutes are all sane. Under 5 mostly burns API quota: the freshness criterion already gives full credit for anything under two hours old, so a five-minute head start rarely changes the score.

**2. Load Config** — `code`

Builds the rubric and the caps from environment variables and emits one item. The single place configuration enters the workflow.

**3. Caps Preflight** — `code` (embeds `code-nodes/caps.js`)

Loads persisted state from `$getWorkflowStaticData('global').caps` and calls `startRun()`. This is where the circuit breaker and the rolling spend window are evaluated.

It deliberately **does not throw**. A thrown error inside a preflight is invisible unless you go reading execution logs; instead it sets `blocked: true` with `blocked_cap` and `blocked_reason`, so the next node can route to a Slack alert that tells you what stopped and why. It also stamps `run_started_at`, which is what the wall-clock cap measures against later.

**4. Caps OK?** — `if`

True → run. False → the breaker/cap alert branch. Both branches are wired; a workflow with a dangling false branch is a workflow that fails silently.

**5. Upwork GraphQL Search** — `httpRequest`

`POST https://api.upwork.com/graphql` over the OAuth2 credential. One query, `marketplaceJobPostingsSearch`, sorted by recency. `onError` is `continueErrorOutput`, so an API failure routes to the breaker alert rather than killing the execution silently.

This is the **only** HTTP node in the workflow, and that is enforced by a test. If you find yourself adding a second one, stop and re-read the safety model in the root README.

**6. Normalize Jobs** — `code`

Maps the GraphQL response onto the flat shape the scorer expects. This is the only node that should ever need touching when Upwork renames a field:

| Internal field              | GraphQL source                                                              |
| --------------------------- | --------------------------------------------------------------------------- |
| `id`                        | `node.id` (falls back to `ciphertext`)                                      |
| `title`, `description`      | `node.title`, `node.description`                                            |
| `skills`                    | `node.skills[].name`                                                        |
| `job_type`                  | `hourly` if `node.job.contractTerms.contractType == "HOURLY"`, else `fixed` |
| `budget`                    | `node.amount.rawValue`                                                      |
| `hourly_min` / `hourly_max` | `node.hourlyBudgetMin/Max.rawValue`                                         |
| `proposals`                 | `node.totalApplicants`                                                      |
| `posted_at`                 | `node.publishedDateTime` (falls back to `createdDateTime`)                  |
| `url`                       | `https://www.upwork.com/jobs/` + `node.ciphertext`                          |
| `client.country`            | `node.client.location.country`                                              |
| `client.hire_rate`          | `client.totalHires / client.totalPostedJobs`                                |
| `client.total_spend`        | `client.totalSpent.rawValue`                                                |
| `client.payment_verified`   | `client.verificationStatus == "VERIFIED"`                                   |

Every field degrades to `null` rather than throwing, and the scorer has a defined behaviour for `null` on every criterion (`tests/test_scoring.py::TestMalformedInput`). Real API payloads have holes in them.

**7. Score Jobs** — `code` (embeds `code-nodes/score.js` + `caps.js`)

Scores each job and counts it as one step against `max_steps_per_run`, re-checking the wall clock as it goes. If a cap hits mid-batch it stops there and records why rather than dropping the work silently.

The embedded scorer is byte-identical to `code-nodes/score.js`, which `tests/test_parity.py` runs against the Python scorer over the same fixtures. Same numbers, same reason strings, or the build fails.

**8. Passed Threshold?** — `filter`

Keeps `result.passed === true`. Everything else is dropped. Disqualified jobs never reach Slack — but they are still counted in the daily digest, so you can see what your rubric is rejecting.

**9. Skip Already Alerted** — `code`

One alert per job id, ever. Keeps the last 2,000 ids in static data so a restart or a re-run does not re-spam the channel.

**10. Format Slack Alert** — `code`

Builds Block Kit: the score, the client facts, the per-criterion breakdown, a draft opener, and a footer stating that nothing was submitted. Mirrors `scoring/slack.py`.

The draft opener is a **template**, not a model call, which is why a default install costs nothing to run. If you want richer drafts, this is the node to put an LLM into — and that is exactly the moment the spend cap starts earning its keep.

**11. Post Alert to Slack** — `slack`

The only outbound side effect in the entire workflow: a message to a human.

**12. Persist State** — `code`

Calls `recordSuccess()` (clearing the error streak), writes the caps state back, and appends up to 500 rows of history for the digest.

## The breaker path

**Build Breaker Alert** → **Post Breaker Alert**

Reached from two places that mean opposite things, and the node treats them differently:

- **The preflight blocked the run** (`blocked: true`). The guard already refused it. Counting that as a fresh error would let a tripped breaker inflate its own counter, so this path only reports.
- **The Upwork call errored** (the HTTP node's `onError: continueErrorOutput` branch). That is a real failure, and this is the **only** node in the workflow that calls `recordError()`. Without it the breaker's counter would never advance and the breaker could never trip, no matter how often the API fell over. `tests/test_workflow.py` asserts exactly one node calls `recordError()` and that the HTTP error branch routes to it.

Either way it posts which cap or error stopped the run, the reason, the consecutive-error count, the current spend, and whether the breaker is now open. When it is open, the message says how to reset it by hand.

## The digest path

**Daily 08:00** → **Build Digest** → **Post Digest to Slack**

Summarises the last 24 hours from persisted history: how many scored, how many passed, how many were disqualified and for which reasons, plus caps headroom. A day where nothing passed says so in words — "that is a normal day, not a broken radar" — because a silent channel is ambiguous and an explicit zero is not.

## Before you activate it

1. Import, then open every node with a `REPLACE_ME` credential and pick the real one.
2. Set `SLACK_CHANNEL_ID` and confirm the bot is in that channel.
3. Set `RADAR_SEARCH_QUERY` to your actual niche. The default is a placeholder.
4. **Execute the workflow manually once** and read the output of Score Jobs before you let a schedule touch it.
5. Consider setting `RADAR_ALERT_THRESHOLD` to something low (say 40) for the first day, so you can see what the rubric does to real jobs, then raise it.
6. Only then activate.
