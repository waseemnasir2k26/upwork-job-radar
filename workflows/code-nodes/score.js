/**
 * JS twin of scoring/score.py.
 *
 * This exact file is embedded into the "Score Jobs" Code node of
 * workflows/upwork-job-radar.json by scripts/build_workflow.py. Do not edit
 * the copy inside the workflow JSON - edit this file and rebuild.
 *
 * tests/test_parity.py runs this under Node against the same fixtures as the
 * Python scorer and fails if a single number or reason string differs, so the
 * logic you review in Python is provably the logic n8n runs.
 *
 * Read-only: this file scores jobs. It never calls Upwork.
 */

const DEFAULT_RUBRIC = {
  alert_threshold: 70,
  unknown_client_policy: "neutral",
  weights: {
    client_country: 10,
    hire_rate: 15,
    client_spend: 12,
    payment_verified: 8,
    budget: 20,
    proposals: 15,
    keywords: 12,
    freshness: 8,
  },
  client_country: {
    allowlist: [
      "US",
      "CA",
      "GB",
      "AU",
      "NZ",
      "IE",
      "DE",
      "NL",
      "SE",
      "NO",
      "DK",
      "FI",
      "CH",
      "AT",
      "BE",
      "FR",
      "SG",
      "AE",
    ],
    blocklist: [],
    hard_gate: true,
  },
  hire_rate: { target: 0.6, hard_fail_below: 0.4, hard_gate: true },
  client_spend: { target_usd: 10000, hard_fail_below_usd: 0, hard_gate: false },
  payment_verified: { required: true },
  budget: {
    fixed_floor_usd: 500,
    fixed_target_usd: 3000,
    hourly_floor_usd: 35,
    hourly_target_usd: 75,
    hard_gate: true,
    missing_budget_policy: "neutral",
  },
  proposals: { ideal_max: 5, zero_at: 50 },
  keywords: {
    must_have_any: [],
    must_not_have: ["unpaid", "revenue share only", "equity only", "no budget"],
    bonus: [
      "n8n",
      "automation",
      "workflow",
      "api integration",
      "wordpress",
      "shopify",
      "openai",
      "anthropic",
      "webhook",
    ],
    hard_gate: true,
  },
  freshness: { fresh_hours: 2, stale_hours: 48 },
};

function roundHalfUp(value, digits) {
  const d = digits || 0;
  const factor = Math.pow(10, d);
  const scaled = Math.floor(Math.abs(value) * factor + 0.5) / factor;
  return value < 0 ? -scaled : scaled;
}

function mergeRubric(base, override) {
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

function clamp(value, low, high) {
  const lo = low === undefined ? 0 : low;
  const hi = high === undefined ? 1 : high;
  return Math.max(lo, Math.min(hi, value));
}

function ramp(value, zeroAt, fullAt) {
  if (fullAt === zeroAt) return value >= fullAt ? 1 : 0;
  return clamp((value - zeroAt) / (fullAt - zeroAt));
}

function parseTs(value) {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  let text = String(value).trim();
  // Match Python's fromisoformat + "naive means UTC" behaviour.
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) text += "Z";
  const ms = Date.parse(text);
  return isNaN(ms) ? null : new Date(ms);
}

function haystack(job) {
  const skills = (job.skills || []).map(String).join(" ");
  return [String(job.title || ""), String(job.description || ""), skills]
    .join(" ")
    .toLowerCase();
}

function pct(value) {
  if (value === null || value === undefined) return null;
  const number = Number(value);
  if (isNaN(number)) return null;
  return number > 1 ? number / 100 : number;
}

function money(value) {
  const n = Math.trunc(roundHalfUp(value, 0));
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function pctStr(value) {
  return money(value * 100) + "%";
}

function hours(value) {
  return roundHalfUp(value, 1).toFixed(1);
}

function crit(key, earned, maximum, reason) {
  return {
    key: key,
    earned: roundHalfUp(earned, 2),
    maximum: roundHalfUp(maximum, 2),
    reason: reason,
  };
}

// -- criteria ---------------------------------------------------------------

function cCountry(job, rub, weight, disq) {
  const cfg = rub.client_country;
  const raw = (job.client || {}).country;
  const code = raw ? String(raw).toUpperCase() : null;
  const allow = (cfg.allowlist || []).map((c) => String(c).toUpperCase());
  const block = (cfg.blocklist || []).map((c) => String(c).toUpperCase());
  if (code === null) {
    return crit(
      "client_country",
      weight * 0.5,
      weight,
      "client country unknown - half credit",
    );
  }
  if (block.indexOf(code) !== -1) {
    if (cfg.hard_gate)
      disq.push("client country " + code + " is on the blocklist");
    return crit("client_country", 0, weight, code + " is blocklisted");
  }
  if (allow.length && allow.indexOf(code) === -1) {
    if (cfg.hard_gate)
      disq.push("client country " + code + " is not on the allowlist");
    return crit("client_country", 0, weight, code + " is not on the allowlist");
  }
  return crit("client_country", weight, weight, code + " allowed");
}

function cHireRate(job, rub, weight, disq) {
  const cfg = rub.hire_rate;
  const rate = pct((job.client || {}).hire_rate);
  if (rate === null) {
    const policy = rub.unknown_client_policy || "neutral";
    if (policy === "disqualify") {
      disq.push("client has no hire-rate history");
      return crit("hire_rate", 0, weight, "no hire history");
    }
    const share = policy === "penalize" ? 0 : 0.5;
    return crit(
      "hire_rate",
      weight * share,
      weight,
      "no hire history - policy '" + policy + "'",
    );
  }
  const floor = Number(cfg.hard_fail_below || 0);
  if (rate < floor) {
    if (cfg.hard_gate) {
      disq.push(
        "hire rate " +
          pctStr(rate) +
          " is below the " +
          pctStr(floor) +
          " floor",
      );
    }
    return crit(
      "hire_rate",
      0,
      weight,
      pctStr(rate) + " below " + pctStr(floor) + " floor",
    );
  }
  const share = ramp(
    rate,
    floor,
    Number(cfg.target === undefined ? 1 : cfg.target),
  );
  return crit("hire_rate", weight * share, weight, pctStr(rate) + " hire rate");
}

function cSpend(job, rub, weight, disq) {
  const cfg = rub.client_spend;
  const rawSpend = (job.client || {}).total_spend;
  if (rawSpend === null || rawSpend === undefined) {
    const policy = rub.unknown_client_policy || "neutral";
    if (policy === "disqualify") {
      disq.push("client spend unknown");
      return crit("client_spend", 0, weight, "spend unknown");
    }
    const share = policy === "penalize" ? 0 : 0.5;
    return crit(
      "client_spend",
      weight * share,
      weight,
      "spend unknown - policy '" + policy + "'",
    );
  }
  const spend = Number(rawSpend);
  const floor = Number(cfg.hard_fail_below_usd || 0);
  if (spend < floor) {
    if (cfg.hard_gate) {
      disq.push("client spend $" + money(spend) + " below $" + money(floor));
    }
    return crit("client_spend", 0, weight, "$" + money(spend) + " below floor");
  }
  const share = ramp(spend, floor, Number(cfg.target_usd || 1));
  return crit(
    "client_spend",
    weight * share,
    weight,
    "$" + money(spend) + " lifetime spend",
  );
}

function cPayment(job, rub, weight, disq) {
  const cfg = rub.payment_verified;
  const verified = (job.client || {}).payment_verified;
  if (verified === null || verified === undefined) {
    return crit(
      "payment_verified",
      weight * 0.5,
      weight,
      "verification status unknown",
    );
  }
  if (!verified) {
    if (cfg.required) disq.push("client payment method is not verified");
    return crit("payment_verified", 0, weight, "not verified");
  }
  return crit("payment_verified", weight, weight, "verified");
}

function cBudget(job, rub, weight, disq) {
  const cfg = rub.budget;
  const jobType = String(job.job_type || "fixed").toLowerCase();
  let rate;
  let floor;
  let target;
  let label;
  if (jobType === "hourly") {
    rate = job.hourly_max || job.hourly_min;
    floor = Number(cfg.hourly_floor_usd || 0);
    target = Number(cfg.hourly_target_usd || 1);
    label = "/hr";
  } else {
    rate = job.budget;
    floor = Number(cfg.fixed_floor_usd || 0);
    target = Number(cfg.fixed_target_usd || 1);
    label = " fixed";
  }
  if (rate === null || rate === undefined) {
    const policy = cfg.missing_budget_policy || "neutral";
    if (policy === "disqualify") {
      disq.push("job has no stated budget");
      return crit("budget", 0, weight, "no budget stated");
    }
    const share = policy === "penalize" ? 0 : 0.5;
    return crit(
      "budget",
      weight * share,
      weight,
      "no budget stated - policy '" + policy + "'",
    );
  }
  rate = Number(rate);
  if (rate < floor) {
    if (cfg.hard_gate) {
      disq.push(
        "$" + money(rate) + label + " is below the $" + money(floor) + " floor",
      );
    }
    return crit(
      "budget",
      0,
      weight,
      "$" + money(rate) + label + " below floor",
    );
  }
  const share = ramp(rate, floor, target);
  return crit("budget", weight * share, weight, "$" + money(rate) + label);
}

function cProposals(job, rub, weight) {
  const cfg = rub.proposals;
  const raw = job.proposals;
  if (raw === null || raw === undefined) {
    return crit("proposals", weight * 0.5, weight, "proposal count unknown");
  }
  const count = Number(raw);
  const ideal = Number(cfg.ideal_max === undefined ? 5 : cfg.ideal_max);
  const zeroAt = Number(cfg.zero_at === undefined ? 50 : cfg.zero_at);
  let share;
  if (count <= ideal) share = 1;
  else if (zeroAt <= ideal) share = 0;
  else share = clamp((zeroAt - count) / (zeroAt - ideal));
  return crit(
    "proposals",
    weight * share,
    weight,
    String(Math.trunc(count)) + " proposals so far",
  );
}

function cKeywords(job, rub, weight, disq) {
  const cfg = rub.keywords;
  const text = haystack(job);
  const banned = (cfg.must_not_have || []).filter(
    (k) => text.indexOf(String(k).toLowerCase()) !== -1,
  );
  if (banned.length) {
    if (cfg.hard_gate) disq.push("banned keyword: " + String(banned[0]));
    return crit(
      "keywords",
      0,
      weight,
      "banned keyword '" + String(banned[0]) + "'",
    );
  }
  const bonusHits = (cfg.bonus || []).filter(
    (k) => text.indexOf(String(k).toLowerCase()) !== -1,
  );
  const must = cfg.must_have_any || [];
  if (must.length) {
    const hits = must.filter(
      (k) => text.indexOf(String(k).toLowerCase()) !== -1,
    );
    if (!hits.length) {
      if (cfg.hard_gate) disq.push("no must-have keyword present");
      return crit("keywords", 0, weight, "no must-have keyword");
    }
    const base = weight * 0.7;
    const extra = weight * 0.3 * clamp(bonusHits.length / 3);
    return crit(
      "keywords",
      base + extra,
      weight,
      "must-have '" +
        String(hits[0]) +
        "' + " +
        bonusHits.length +
        " bonus term(s)",
    );
  }
  if (!bonusHits.length)
    return crit("keywords", 0, weight, "no matching keywords");
  const share = clamp(bonusHits.length / 3);
  return crit(
    "keywords",
    weight * share,
    weight,
    bonusHits.length +
      " bonus term(s): " +
      bonusHits.slice(0, 3).map(String).join(", "),
  );
}

function cFreshness(job, rub, weight, disq, now) {
  const cfg = rub.freshness;
  const posted = parseTs(job.posted_at);
  if (posted === null)
    return crit("freshness", weight * 0.5, weight, "post time unknown");
  let ageHours = (now.getTime() - posted.getTime()) / 3600000;
  if (ageHours < 0) ageHours = 0;
  const fresh = Number(cfg.fresh_hours === undefined ? 2 : cfg.fresh_hours);
  const stale = Number(cfg.stale_hours === undefined ? 48 : cfg.stale_hours);
  let share;
  if (ageHours <= fresh) share = 1;
  else if (stale <= fresh) share = 0;
  else share = clamp((stale - ageHours) / (stale - fresh));
  return crit(
    "freshness",
    weight * share,
    weight,
    "posted " + hours(ageHours) + "h ago",
  );
}

// -- public -----------------------------------------------------------------

function scoreJob(job, rubric, now) {
  const rub = mergeRubric(DEFAULT_RUBRIC, rubric);
  const when = now ? (now instanceof Date ? now : new Date(now)) : new Date();
  const weights = rub.weights || {};
  const disq = [];

  const handlers = [
    ["client_country", (w) => cCountry(job, rub, w, disq)],
    ["hire_rate", (w) => cHireRate(job, rub, w, disq)],
    ["client_spend", (w) => cSpend(job, rub, w, disq)],
    ["payment_verified", (w) => cPayment(job, rub, w, disq)],
    ["budget", (w) => cBudget(job, rub, w, disq)],
    ["proposals", (w) => cProposals(job, rub, w, disq)],
    ["keywords", (w) => cKeywords(job, rub, w, disq)],
    ["freshness", (w) => cFreshness(job, rub, w, disq, when)],
  ];

  const breakdown = [];
  let earned = 0;
  let possible = 0;
  handlers.forEach((pair) => {
    const weight = Number(weights[pair[0]] || 0);
    if (weight <= 0) return;
    const result = pair[1](weight);
    breakdown.push(result);
    earned += result.earned;
    possible += result.maximum;
  });

  const raw =
    possible <= 0 ? 0 : Math.trunc(roundHalfUp((earned / possible) * 100, 0));
  const score = disq.length ? 0 : raw;
  const threshold = Number(
    rub.alert_threshold === undefined ? 70 : rub.alert_threshold,
  );

  return {
    id: job.id === undefined ? null : job.id,
    title: job.title === undefined ? null : job.title,
    url: job.url === undefined ? null : job.url,
    score: score,
    raw_score: raw,
    threshold: threshold,
    disqualified: disq.length > 0,
    disqualifiers: disq,
    passed: disq.length === 0 && score >= threshold,
    breakdown: breakdown,
  };
}

function scoreJobs(jobs, rubric, now) {
  const results = (jobs || []).map((job) => scoreJob(job, rubric, now));
  results.sort((a, b) => b.score - a.score);
  return results;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { DEFAULT_RUBRIC, scoreJob, scoreJobs, roundHalfUp };
}
