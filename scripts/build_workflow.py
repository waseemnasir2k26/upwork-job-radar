"""Build workflows/upwork-job-radar.json from the reviewable JS in code-nodes/.

    python scripts/build_workflow.py           # write the workflow JSON
    python scripts/build_workflow.py --check   # exit 1 if the file is stale

The Code nodes are not hand-written inside the JSON blob. They are assembled
here from workflows/code-nodes/*.js, so the logic you can read, lint and
parity-test is byte-for-byte the logic n8n executes. tests/test_workflow.py
runs --check, which means a stale workflow JSON fails the test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_DIR = os.path.join(ROOT, "workflows", "code-nodes")
OUT_PATH = os.path.join(ROOT, "workflows", "upwork-job-radar.json")


def _read(name: str) -> str:
    with open(os.path.join(CODE_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read().rstrip() + "\n"


SCORE_JS = "score.js"
CAPS_JS = "caps.js"


# --------------------------------------------------------------------------
# Code node bodies. Each is <library>.js + a small n8n-specific runner.
# --------------------------------------------------------------------------

LOAD_CONFIG_RUNNER = r"""
// Load Config - the single place the run reads its rubric and its caps.
// Everything is overridable by env var so caps live in config, never in a prompt.
const num = (v, d) => (v === undefined || v === null || v === '' ? d : Number(v));

const rubricOverride = $env.RADAR_RUBRIC_JSON ? JSON.parse($env.RADAR_RUBRIC_JSON) : {};
const capsOverride = $env.RADAR_CAPS_JSON ? JSON.parse($env.RADAR_CAPS_JSON) : {};

const caps = Object.assign({
  max_steps_per_run: num($env.RADAR_MAX_STEPS_PER_RUN, 200),
  wall_clock_seconds: num($env.RADAR_WALL_CLOCK_SECONDS, 120),
  spend: {
    cap_usd: num($env.RADAR_SPEND_CAP_USD, 5),
    window_hours: num($env.RADAR_SPEND_WINDOW_HOURS, 24),
  },
  circuit_breaker: {
    consecutive_errors: num($env.RADAR_BREAKER_CONSECUTIVE_ERRORS, 3),
    cooldown_minutes: num($env.RADAR_BREAKER_COOLDOWN_MINUTES, 60),
    auto_disable: true,
  },
}, capsOverride);

const rubric = Object.assign({
  alert_threshold: num($env.RADAR_ALERT_THRESHOLD, 70),
}, rubricOverride);

return [{
  json: {
    rubric,
    caps,
    search_query: $env.RADAR_SEARCH_QUERY || 'n8n OR automation OR "api integration"',
    page_size: num($env.RADAR_PAGE_SIZE, 50),
    slack_channel: $env.SLACK_CHANNEL_ID || '',
  },
}];
""".strip() + "\n"


CAPS_PREFLIGHT_RUNNER = r"""
// --- n8n runner: caps preflight -------------------------------------------
// Reads persisted state from workflow static data, opens the run, and either
// lets it through or short-circuits to the circuit-breaker alert branch.
const staticData = $getWorkflowStaticData('global');
const cfg = $input.first().json;
staticData.caps = staticData.caps || {};

const guard = new CapsGuard(cfg.caps, staticData.caps);
let blocked = false;
let blockedCap = null;
let blockedReason = null;

try {
  guard.startRun();
} catch (err) {
  blocked = true;
  blockedCap = err.cap || 'unknown';
  blockedReason = err.message;
}

staticData.caps = guard.toDict();
staticData.run_started_at = new Date().toISOString();

return [{
  json: Object.assign({}, cfg, {
    blocked,
    blocked_cap: blockedCap,
    blocked_reason: blockedReason,
    caps_state: guard.toDict(),
    caps_remaining: guard.remaining(),
  }),
}];
""".strip() + "\n"


NORMALIZE_RUNNER = r"""
// Normalize Jobs - map the official Upwork GraphQL response into the flat
// shape the scorer expects. Field names follow the public marketplace job
// posting search schema; adjust here (not in the scorer) if Upwork renames a
// field. Nothing is written back to Upwork.
const cfg = $('Caps Preflight').first().json;
const response = $input.first().json;
const edges =
  (((response.data || {}).marketplaceJobPostingsSearch || {}).edges) || [];

const jobs = edges.map((edge) => {
  const n = edge.node || {};
  const client = n.client || {};
  const amount = (n.amount || {}).rawValue;
  const hourlyMin = ((n.hourlyBudgetMin || {}).rawValue);
  const hourlyMax = ((n.hourlyBudgetMax || {}).rawValue);
  const ciphertext = n.ciphertext || n.id || '';
  return {
    id: n.id || ciphertext,
    title: n.title || '',
    description: n.description || '',
    skills: (n.skills || []).map((s) => (s && s.name ? s.name : String(s))),
    job_type: String(n.job && n.job.contractTerms
      && n.job.contractTerms.contractType === 'HOURLY' ? 'hourly' : 'fixed'),
    budget: amount === undefined || amount === null ? null : Number(amount),
    hourly_min: hourlyMin === undefined || hourlyMin === null ? null : Number(hourlyMin),
    hourly_max: hourlyMax === undefined || hourlyMax === null ? null : Number(hourlyMax),
    proposals: n.totalApplicants === undefined ? null : Number(n.totalApplicants),
    posted_at: n.publishedDateTime || n.createdDateTime || null,
    url: ciphertext ? 'https://www.upwork.com/jobs/' + ciphertext : null,
    client: {
      country: (client.location || {}).country || null,
      hire_rate: client.totalHires && client.totalPostedJobs
        ? client.totalHires / client.totalPostedJobs : null,
      total_spend: ((client.totalSpent || {}).rawValue) === undefined
        ? null : Number((client.totalSpent || {}).rawValue),
      payment_verified: client.verificationStatus === undefined
        ? null : client.verificationStatus === 'VERIFIED',
      jobs_posted: client.totalPostedJobs === undefined
        ? null : Number(client.totalPostedJobs),
    },
  };
});

return jobs.map((job) => ({ json: { job, rubric: cfg.rubric, caps: cfg.caps } }));
""".strip() + "\n"


SCORE_RUNNER = r"""
// --- n8n runner: score every job, counting each one against the step cap ---
const staticData = $getWorkflowStaticData('global');
const items = $input.all();
if (!items.length) return [];

const cfg = $('Caps Preflight').first().json;
const guard = new CapsGuard(cfg.caps, staticData.caps);
// Re-anchor the wall clock to when this run actually started, not to now.
guard.runStarted = new Date(staticData.run_started_at || Date.now());

const out = [];
let stoppedBy = null;
for (const item of items) {
  try {
    guard.check(0);
  } catch (err) {
    stoppedBy = err.cap + ': ' + err.message;
    break;
  }
  const job = item.json.job;
  const result = scoreJob(job, item.json.rubric, new Date());
  guard.recordStep(0);
  out.push({ json: { job, result, stopped_by: null } });
}

staticData.caps = guard.toDict();
if (stoppedBy && out.length) out[0].json.stopped_by = stoppedBy;
if (stoppedBy && !out.length) return [{ json: { stopped_by: stoppedBy, result: null } }];
return out;
""".strip() + "\n"


DEDUPE_RUNNER = r"""
// Skip Already Alerted - one alert per job id, ever. Keeps the last 2000 ids
// in workflow static data so a restart does not re-spam the channel.
const staticData = $getWorkflowStaticData('global');
staticData.seen = Array.isArray(staticData.seen) ? staticData.seen : [];
const seen = new Set(staticData.seen);

const fresh = [];
for (const item of $input.all()) {
  const id = item.json.result && item.json.result.id;
  if (!id || seen.has(id)) continue;
  seen.add(id);
  fresh.push(item);
}

staticData.seen = Array.from(seen).slice(-2000);
return fresh;
""".strip() + "\n"


FORMAT_ALERT_RUNNER = r"""
// Format Slack Alert - mirrors scoring/slack.py build_slack_blocks().
// The draft opener is a template, not a model call, so a default install
// spends nothing. Swap in an LLM node here if you want richer drafts - that
// is exactly what the spend cap in Caps Preflight is protecting.
const BAR = 10;
const bar = (earned, maximum) => {
  if (maximum <= 0) return ' '.repeat(BAR);
  const filled = Math.round(BAR * Math.max(0, Math.min(1, earned / maximum)));
  return '#'.repeat(filled) + '.'.repeat(BAR - filled);
};
const money = (v) => (v === null || v === undefined || isNaN(Number(v))
  ? 'n/a'
  : '$' + String(Math.round(Number(v))).replace(/\B(?=(\d{3})+(?!\d))/g, ','));
const budgetLine = (job) => {
  if (String(job.job_type || 'fixed').toLowerCase() === 'hourly') {
    if (job.hourly_min && job.hourly_max) {
      return money(job.hourly_min) + '-' + money(job.hourly_max) + '/hr';
    }
    if (job.hourly_max || job.hourly_min) return money(job.hourly_max || job.hourly_min) + '/hr';
    return 'hourly, rate not stated';
  }
  return job.budget ? money(job.budget) + ' fixed' : 'budget not stated';
};
const draftOpener = (job, result) => {
  const skills = (job.skills || []).slice(0, 3).map(String);
  const skillsTxt = skills.length ? skills.join(', ') : 'the stack you listed';
  const ranked = (result.breakdown || []).filter((b) => b.maximum > 0)
    .sort((a, b) => (b.earned / b.maximum) - (a.earned / a.maximum));
  const angle = ranked.length ? ranked[0].reason : 'the brief';
  return 'Hi - on "' + String(job.title || 'your project').trim() + '": the part I would '
    + 'start with is ' + skillsTxt + ', because that is where this scope usually stalls.\n'
    + 'Two questions before I quote: what does done look like for you, and who owns the '
    + 'accounts/credentials?\nSignal I picked up from the post: ' + angle + '.';
};

return $input.all().map((item) => {
  const job = item.json.job;
  const result = item.json.result;
  const client = job.client || {};
  const lines = (result.breakdown || []).map((b) => '`' + b.key + '` '
    + bar(b.earned, b.maximum) + ' ' + b.earned + '/' + Math.round(b.maximum)
    + ' - ' + b.reason);
  const blocks = [
    { type: 'header',
      text: { type: 'plain_text', text: 'Score ' + result.score + ' - ' + String(job.title || '').slice(0, 120) } },
    { type: 'section',
      fields: [
        { type: 'mrkdwn', text: '*Budget*\n' + budgetLine(job) },
        { type: 'mrkdwn', text: '*Country*\n' + (client.country || 'unknown') },
        { type: 'mrkdwn', text: '*Proposals*\n' + (job.proposals === null || job.proposals === undefined ? 'unknown' : job.proposals) },
        { type: 'mrkdwn', text: '*Client spend*\n' + money(client.total_spend) },
      ] },
    { type: 'section', text: { type: 'mrkdwn', text: '*Why it scored this*\n' + lines.join('\n') } },
    { type: 'section', text: { type: 'mrkdwn', text: '*Draft opener (yours to edit - nothing was sent)*\n>' + draftOpener(job, result).replace(/\n/g, '\n>') } },
    { type: 'context', elements: [{ type: 'mrkdwn',
      text: 'Read-only alert. Nothing was submitted to Upwork. <' + (job.url || 'https://www.upwork.com') + '|Open the job> and apply by hand.' }] },
  ];
  return { json: { text: 'Upwork Job Radar: ' + String(job.title || '').slice(0, 80) + ' scored ' + result.score, blocks } };
});
""".strip() + "\n"


PERSIST_RUNNER = r"""
// Persist State - close the run out cleanly: clear the error streak, roll the
// spend counter forward, and keep a small history for the daily digest.
const staticData = $getWorkflowStaticData('global');
const cfg = $('Caps Preflight').first().json;
const guard = new CapsGuard(cfg.caps, staticData.caps);
guard.recordSuccess();
staticData.caps = guard.toDict();

staticData.history = Array.isArray(staticData.history) ? staticData.history : [];
for (const item of $input.all()) {
  staticData.history.push({
    at: new Date().toISOString(),
    id: item.json.result ? item.json.result.id : null,
    score: item.json.result ? item.json.result.score : null,
    title: item.json.result ? item.json.result.title : null,
    passed: item.json.result ? item.json.result.passed : null,
    disqualifiers: item.json.result ? item.json.result.disqualifiers : [],
  });
}
staticData.history = staticData.history.slice(-500);

return [{ json: { alerted: $input.all().length, caps: guard.remaining() } }];
""".strip() + "\n"


BREAKER_ALERT_RUNNER = r"""
// --- n8n runner: breaker alert --------------------------------------------
// Reached from two places, and they mean opposite things:
//
//   a) Caps Preflight said blocked=true. The guard already refused this run;
//      counting it again would let a tripped breaker inflate its own counter.
//   b) The Upwork call errored (onError: continueErrorOutput). THAT is a real
//      failure and it is the only thing that advances the error streak.
//
// Without this node calling recordError(), the breaker could never trip in
// production no matter how many times the API fell over.
const staticData = $getWorkflowStaticData('global');
const cfg = $input.first().json;
const preflight = $('Caps Preflight').first().json;
const wasRefusedByCaps = cfg.blocked === true;

let state = cfg.caps_state || preflight.caps_state || {};
let cap = cfg.blocked_cap || null;
let reason = cfg.blocked_reason || null;

if (!wasRefusedByCaps) {
  const guard = new CapsGuard(preflight.caps, staticData.caps);
  const message = (cfg.error && (cfg.error.message || cfg.error))
    || 'Upwork API request failed';
  const tripped = guard.recordError(String(message));
  staticData.caps = guard.toDict();
  state = guard.toDict();
  cap = tripped ? 'circuit_breaker' : 'upstream_error';
  reason = tripped
    ? guard.disabledReason
    : String(message) + ' (' + guard.consecutiveErrors + ' consecutive)';
}

return [{
  json: {
    text: 'Upwork Job Radar halted: ' + (reason || 'unknown reason'),
    blocks: [
      { type: 'header', text: { type: 'plain_text', text: 'Job Radar halted - ' + (cap || 'cap') } },
      { type: 'section', text: { type: 'mrkdwn', text: '*Reason*\n' + (reason || 'unknown') } },
      { type: 'section', fields: [
        { type: 'mrkdwn', text: '*Consecutive errors*\n' + (state.consecutive_errors || 0) },
        { type: 'mrkdwn', text: '*Spend this window*\n$' + Number(state.spend_usd || 0).toFixed(4) },
        { type: 'mrkdwn', text: '*Breaker*\n' + (state.disabled ? 'OPEN - workflow disabled' : 'closed') },
      ] },
      { type: 'context', elements: [{ type: 'mrkdwn',
        text: state.disabled
          ? 'The workflow disabled itself. It allows one probe run after the cooldown; fix the cause, or reset it by clearing `caps` in workflow static data.'
          : 'Counted against the error streak. The breaker opens once the streak reaches the configured limit.' }] },
    ],
  },
}];
""".strip() + "\n"


DIGEST_RUNNER = r"""
// Build Digest - a once-a-day summary from the history the runs persisted.
// Mirrors scoring/slack.py render_digest().
const staticData = $getWorkflowStaticData('global');
const history = Array.isArray(staticData.history) ? staticData.history : [];
const since = Date.now() - 24 * 3600 * 1000;
const window = history.filter((h) => Date.parse(h.at) >= since);
const passed = window.filter((h) => h.passed);
const disq = window.filter((h) => (h.disqualifiers || []).length);

const counts = {};
disq.forEach((h) => (h.disqualifiers || []).forEach((d) => {
  const key = String(d).split(':')[0];
  counts[key] = (counts[key] || 0) + 1;
}));
const topReasons = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 5);

const caps = staticData.caps || {};
const lines = [
  '*' + window.length + '* scored | *' + passed.length + '* passed | *' + disq.length + '* disqualified',
  passed.length
    ? passed.slice(0, 10).map((h) => '`' + h.score + '`  ' + String(h.title || '').slice(0, 56)).join('\n')
    : '_Nothing passed today. That is a normal day, not a broken radar._',
];
if (topReasons.length) {
  lines.push('*Top disqualifier reasons*\n'
    + topReasons.map((r) => '`' + counts[r] + 'x`  ' + r).join('\n'));
}
lines.push('_caps: $' + Number(caps.spend_usd || 0).toFixed(4) + ' spent this window | breaker '
  + (caps.disabled ? 'OPEN (disabled)' : 'closed') + '_');

return [{
  json: {
    text: 'Upwork Job Radar daily digest',
    blocks: [
      { type: 'header', text: { type: 'plain_text', text: 'Job Radar - last 24h' } },
      { type: 'section', text: { type: 'mrkdwn', text: lines.join('\n\n') } },
      { type: 'context', elements: [{ type: 'mrkdwn',
        text: 'Read-only. No proposal was submitted and no Connect was spent.' }] },
    ],
  },
}];
""".strip() + "\n"


UPWORK_QUERY = """query marketplaceJobPostings($q: String!, $first: Int!) {
  marketplaceJobPostingsSearch(
    marketPlaceJobFilter: { searchExpression_eq: $q }
    searchType: USER_JOBS_SEARCH
    sortAttributes: [{ field: RECENCY }]
    pagination: { first: $first }
  ) {
    edges {
      node {
        id ciphertext title description publishedDateTime createdDateTime
        totalApplicants
        skills { name }
        amount { rawValue }
        hourlyBudgetMin { rawValue }
        hourlyBudgetMax { rawValue }
        job { contractTerms { contractType } }
        client {
          totalHires totalPostedJobs verificationStatus
          location { country }
          totalSpent { rawValue }
        }
      }
    }
  }
}"""


def _code_node(name: str, position, code: str, note: str, mode: str = "runOnceForAllItems"):
    return {
        "parameters": {"mode": mode, "jsCode": code},
        "id": name.lower().replace(" ", "-").replace("?", ""),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": position,
        "notes": note,
        "notesInFlow": False,
    }


def _slack_node(name: str, position, note: str):
    return {
        "parameters": {
            "select": "channel",
            "channelId": {
                "__rl": True,
                "value": "={{ $env.SLACK_CHANNEL_ID }}",
                "mode": "id",
            },
            "messageType": "block",
            "blocksUi": "={{ JSON.stringify($json.blocks) }}",
            "text": "={{ $json.text }}",
            "otherOptions": {"includeLinkToWorkflow": False},
        },
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "n8n-nodes-base.slack",
        "typeVersion": 2.2,
        "position": position,
        "notes": note,
        "credentials": {"slackApi": {"id": "REPLACE_ME", "name": "Slack account"}},
    }


def build() -> dict:
    score_js = _read(SCORE_JS)
    caps_js = _read(CAPS_JS)

    nodes = [
        {
            "parameters": {
                "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}
            },
            "id": "schedule-poll",
            "name": "Every 10 Minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [-460, 0],
            "notes": "Poll interval. 5 / 10 / 20 minutes are all sane; "
                     "anything under 5 mostly burns API quota.",
        },
        _code_node(
            "Load Config", [-240, 0], LOAD_CONFIG_RUNNER,
            "Reads the rubric and all three caps from env vars. Caps live in "
            "config, never in a prompt."),
        _code_node(
            "Caps Preflight", [-20, 0],
            caps_js + "\n// --- n8n runner ---\n" + CAPS_PREFLIGHT_RUNNER,
            "Circuit breaker + spend window + run clock. Sets blocked=true "
            "instead of throwing, so the false branch can alert."),
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "version": 2,
                                "leftValue": "", "typeValidation": "strict"},
                    "conditions": [{
                        "id": "caps-ok",
                        "leftValue": "={{ $json.blocked }}",
                        "rightValue": False,
                        "operator": {"type": "boolean", "operation": "false",
                                     "singleValue": True},
                    }],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": "caps-ok",
            "name": "Caps OK?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [200, 0],
            "notes": "True = run. False = the breaker or a cap stopped this run.",
        },
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.upwork.com/graphql",
                "authentication": "genericCredentialType",
                "genericAuthType": "oAuth2Api",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify({ query: "
                            + json.dumps(UPWORK_QUERY)
                            + ", variables: { q: $json.search_query, first: $json.page_size } }) }}",
                "options": {"timeout": 20000},
            },
            "id": "upwork-search",
            "name": "Upwork GraphQL Search",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [420, -110],
            "notes": "Official Upwork GraphQL API over OAuth2. READ ONLY - this "
                     "is a search query and nothing else. Never point this at a "
                     "browser-automation endpoint.",
            "credentials": {"oAuth2Api": {"id": "REPLACE_ME",
                                          "name": "Upwork OAuth2 API"}},
            "onError": "continueErrorOutput",
        },
        _code_node(
            "Normalize Jobs", [640, -110], NORMALIZE_RUNNER,
            "Maps the GraphQL response onto the flat job shape the scorer "
            "expects. Change field mapping here, never in the scorer."),
        _code_node(
            "Score Jobs", [860, -110],
            score_js + "\n" + caps_js + "\n// --- n8n runner ---\n" + SCORE_RUNNER,
            "Embedded copy of workflows/code-nodes/score.js + caps.js. Built by "
            "scripts/build_workflow.py - do not edit here."),
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "version": 2,
                                "leftValue": "", "typeValidation": "strict"},
                    "conditions": [{
                        "id": "passed",
                        "leftValue": "={{ $json.result.passed }}",
                        "rightValue": True,
                        "operator": {"type": "boolean", "operation": "true",
                                     "singleValue": True},
                    }],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": "passed-threshold",
            "name": "Passed Threshold?",
            "type": "n8n-nodes-base.filter",
            "typeVersion": 2,
            "position": [1080, -110],
            "notes": "Only jobs at or above the configured alert threshold "
                     "continue. Everything else is dropped silently.",
        },
        _code_node(
            "Skip Already Alerted", [1300, -110], DEDUPE_RUNNER,
            "One alert per job id, ever. Static data survives restarts."),
        _code_node(
            "Format Slack Alert", [1520, -110], FORMAT_ALERT_RUNNER,
            "Renders the score breakdown + a DRAFT opener. Nothing is sent to "
            "Upwork here or anywhere else."),
        _slack_node(
            "Post Alert to Slack", [1740, -110],
            "The only outbound side effect in the whole workflow: a Slack "
            "message to a human."),
        _code_node(
            "Persist State", [1960, -110], caps_js + "\n// --- n8n runner ---\n"
            + PERSIST_RUNNER,
            "Clears the error streak, rolls the spend window, keeps 500 rows of "
            "history for the digest."),
        _code_node(
            "Build Breaker Alert", [420, 140],
            caps_js + "\n// --- n8n runner ---\n" + BREAKER_ALERT_RUNNER,
            "Explains which cap stopped the run. Also the ONLY node that calls "
            "recordError() - without it the breaker could never trip."),
        _slack_node(
            "Post Breaker Alert", [640, 140],
            "Fires when the breaker is open or a cap was hit."),
        {
            "parameters": {
                "rule": {"interval": [{"field": "cronExpression",
                                       "expression": "0 8 * * *"}]}
            },
            "id": "schedule-digest",
            "name": "Daily 08:00",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [-460, 320],
            "notes": "Daily digest trigger. Server timezone.",
        },
        _code_node(
            "Build Digest", [-240, 320], DIGEST_RUNNER,
            "Summarises the last 24h from persisted history, including caps "
            "headroom."),
        _slack_node(
            "Post Digest to Slack", [-20, 320],
            "One digest a day, whether or not anything passed."),
    ]

    connections = {
        "Every 10 Minutes": {"main": [[{"node": "Load Config", "type": "main", "index": 0}]]},
        "Load Config": {"main": [[{"node": "Caps Preflight", "type": "main", "index": 0}]]},
        "Caps Preflight": {"main": [[{"node": "Caps OK?", "type": "main", "index": 0}]]},
        "Caps OK?": {"main": [
            [{"node": "Upwork GraphQL Search", "type": "main", "index": 0}],
            [{"node": "Build Breaker Alert", "type": "main", "index": 0}],
        ]},
        "Upwork GraphQL Search": {"main": [
            [{"node": "Normalize Jobs", "type": "main", "index": 0}],
            [{"node": "Build Breaker Alert", "type": "main", "index": 0}],
        ]},
        "Normalize Jobs": {"main": [[{"node": "Score Jobs", "type": "main", "index": 0}]]},
        "Score Jobs": {"main": [[{"node": "Passed Threshold?", "type": "main", "index": 0}]]},
        "Passed Threshold?": {"main": [[{"node": "Skip Already Alerted", "type": "main", "index": 0}]]},
        "Skip Already Alerted": {"main": [[{"node": "Format Slack Alert", "type": "main", "index": 0}]]},
        "Format Slack Alert": {"main": [[{"node": "Post Alert to Slack", "type": "main", "index": 0}]]},
        "Post Alert to Slack": {"main": [[{"node": "Persist State", "type": "main", "index": 0}]]},
        "Build Breaker Alert": {"main": [[{"node": "Post Breaker Alert", "type": "main", "index": 0}]]},
        "Daily 08:00": {"main": [[{"node": "Build Digest", "type": "main", "index": 0}]]},
        "Build Digest": {"main": [[{"node": "Post Digest to Slack", "type": "main", "index": 0}]]},
    }

    return {
        "name": "Upwork Job Radar",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "saveExecutionProgress": True,
            "executionTimeout": 300,
            "timezone": "UTC",
        },
        "pinData": {},
        "tags": [],
        "meta": {
            "description": (
                "Read-only Upwork job radar. Polls the official API, scores "
                "each job against a configurable rubric, alerts a human in "
                "Slack. Never submits a proposal, never sends a message, never "
                "spends a Connect."
            ),
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the n8n workflow JSON.")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed JSON is stale")
    args = parser.parse_args(argv)

    workflow = build()
    text = json.dumps(workflow, indent=2, ensure_ascii=False) + "\n"

    if args.check:
        if not os.path.exists(OUT_PATH):
            print("MISSING: " + OUT_PATH)
            return 1
        with open(OUT_PATH, "r", encoding="utf-8") as handle:
            current = handle.read()
        if current != text:
            print("STALE: " + OUT_PATH +
                  " does not match code-nodes/*.js - run scripts/build_workflow.py")
            return 1
        print("OK: workflow JSON is in sync with code-nodes/*.js")
        return 0

    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote " + OUT_PATH + " (" + str(len(workflow["nodes"])) + " nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
