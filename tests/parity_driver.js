/**
 * Node driver used by tests/test_parity.py.
 *
 * Runs the JS twins (the exact files embedded into the n8n Code nodes)
 * against the same inputs as the Python modules and prints JSON on stdout,
 * so Python can deep-compare the two implementations.
 *
 *   node tests/parity_driver.js score <jobsFile> <nowIso>
 *   node tests/parity_driver.js caps  <scriptFile>
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const { scoreJobs } = require(
  path.join(ROOT, "workflows", "code-nodes", "score.js"),
);
const { CapsGuard } = require(
  path.join(ROOT, "workflows", "code-nodes", "caps.js"),
);

const mode = process.argv[2];

if (mode === "score") {
  const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  const jobs = Array.isArray(payload) ? payload : payload.jobs;
  const rubric = payload.rubric || null;
  const now = new Date(process.argv[4]);
  process.stdout.write(JSON.stringify(scoreJobs(jobs, rubric, now)));
  process.exit(0);
}

if (mode === "caps") {
  // A scripted sequence: [{op, ...}] driven by a fake clock, mirroring the
  // Python side exactly. Emits the state after every operation.
  const script = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  let now = new Date(script.start);
  const guard = new CapsGuard(script.caps, script.state || {}, () => now);
  const trace = [];

  script.ops.forEach((op) => {
    const entry = { op: op.op, error: null };
    try {
      if (op.op === "advance") {
        now = new Date(now.getTime() + op.seconds * 1000);
      } else if (op.op === "start_run") {
        guard.startRun();
      } else if (op.op === "check") {
        guard.check(op.cost || 0);
      } else if (op.op === "record_step") {
        guard.recordStep(op.cost || 0);
      } else if (op.op === "record_error") {
        entry.tripped = guard.recordError(op.message || "");
      } else if (op.op === "record_success") {
        guard.recordSuccess();
      } else if (op.op === "reset") {
        guard.reset();
      } else {
        throw new Error("unknown op: " + op.op);
      }
    } catch (err) {
      entry.error = { cap: err.cap || null, message: err.message };
    }
    entry.state = guard.toDict();
    entry.remaining = guard.remaining();
    trace.push(entry);
  });

  process.stdout.write(JSON.stringify(trace));
  process.exit(0);
}

process.stderr.write("unknown mode: " + mode + "\n");
process.exit(2);
