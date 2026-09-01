"""Workflow JSON tests.

Two jobs here:

1. Structural - the file is importable n8n JSON: a nodes array, a connections
   object that only points at nodes that exist, and settings.
2. Safety - the workflow cannot quietly grow a write path to Upwork. There is
   exactly one HTTP node, it is a GraphQL query, and nothing in the file
   contains a mutation or a credential value.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.helpers import ROOT

WORKFLOW_PATH = os.path.join(ROOT, "workflows", "upwork-job-radar.json")
BUILDER = os.path.join(ROOT, "scripts", "build_workflow.py")
CODE_DIR = os.path.join(ROOT, "workflows", "code-nodes")
NODE = shutil.which("node")

with open(WORKFLOW_PATH, "r", encoding="utf-8") as _handle:
    WORKFLOW = json.load(_handle)

NODES = WORKFLOW["nodes"]
NAMES = [n["name"] for n in NODES]
CODE_NODES = [n for n in NODES if n["type"] == "n8n-nodes-base.code"]


class TestStructure(unittest.TestCase):

    def test_top_level_keys(self):
        for key in ("name", "nodes", "connections", "settings"):
            self.assertIn(key, WORKFLOW)
        self.assertIsInstance(WORKFLOW["nodes"], list)
        self.assertIsInstance(WORKFLOW["connections"], dict)
        self.assertIsInstance(WORKFLOW["settings"], dict)

    def test_ships_inactive(self):
        self.assertFalse(WORKFLOW["active"],
                         "an imported workflow must never start already running")

    def test_execution_order_and_timeout_are_set(self):
        self.assertEqual(WORKFLOW["settings"]["executionOrder"], "v1")
        self.assertGreater(WORKFLOW["settings"]["executionTimeout"], 0)

    def test_node_names_are_unique(self):
        self.assertEqual(len(NAMES), len(set(NAMES)))

    def test_every_node_has_the_required_fields(self):
        for node in NODES:
            with self.subTest(node=node.get("name")):
                for key in ("name", "type", "typeVersion", "position", "parameters"):
                    self.assertIn(key, node)
                self.assertEqual(len(node["position"]), 2)

    def test_every_connection_points_at_a_real_node(self):
        for source, spec in WORKFLOW["connections"].items():
            self.assertIn(source, NAMES, "unknown source node " + source)
            for branch in spec["main"]:
                for link in branch:
                    self.assertIn(link["node"], NAMES,
                                  "unknown target node " + link["node"])

    def test_both_triggers_are_present(self):
        triggers = [n for n in NODES if n["type"] == "n8n-nodes-base.scheduleTrigger"]
        self.assertEqual(len(triggers), 2, "poll trigger + daily digest trigger")

    def test_every_non_trigger_node_is_reachable(self):
        reachable = set()
        edges = {s: [l["node"] for b in spec["main"] for l in b]
                 for s, spec in WORKFLOW["connections"].items()}
        stack = [n["name"] for n in NODES
                 if n["type"] == "n8n-nodes-base.scheduleTrigger"]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(edges.get(current, []))
        self.assertEqual(set(NAMES), reachable,
                         "orphan nodes: " + str(set(NAMES) - reachable))

    def test_the_if_node_has_both_branches_wired(self):
        branches = WORKFLOW["connections"]["Caps OK?"]["main"]
        self.assertEqual(len(branches), 2, "true branch and false branch")
        self.assertTrue(branches[0] and branches[1])

    def test_every_node_carries_a_note(self):
        for node in NODES:
            with self.subTest(node=node["name"]):
                self.assertTrue(node.get("notes"),
                                node["name"] + " has no explanatory note")


class TestEmbeddedCode(unittest.TestCase):

    def test_workflow_json_is_in_sync_with_the_js_sources(self):
        proc = subprocess.run(["python", BUILDER, "--check"],
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)

    def test_score_node_embeds_the_reviewable_scorer_verbatim(self):
        with open(os.path.join(CODE_DIR, "score.js"), "r", encoding="utf-8") as h:
            source = h.read().rstrip()
        node = next(n for n in NODES if n["name"] == "Score Jobs")
        self.assertIn(source, node["parameters"]["jsCode"])

    def test_caps_nodes_embed_the_reviewable_guard_verbatim(self):
        with open(os.path.join(CODE_DIR, "caps.js"), "r", encoding="utf-8") as h:
            source = h.read().rstrip()
        for name in ("Caps Preflight", "Score Jobs", "Persist State"):
            with self.subTest(node=name):
                node = next(n for n in NODES if n["name"] == name)
                self.assertIn(source, node["parameters"]["jsCode"])

    def test_every_code_node_has_a_body(self):
        self.assertGreaterEqual(len(CODE_NODES), 8)
        for node in CODE_NODES:
            with self.subTest(node=node["name"]):
                self.assertTrue(node["parameters"]["jsCode"].strip())
                self.assertIn("return", node["parameters"]["jsCode"])

    @unittest.skipIf(NODE is None, "node is not installed - cannot syntax check")
    def test_every_code_node_is_valid_javascript(self):
        for node in CODE_NODES:
            with self.subTest(node=node["name"]):
                # n8n wraps Code node bodies in a function, which is what makes
                # a top-level `return` legal. Reproduce that before checking.
                wrapped = ("async function __n8nCodeNode() {\n"
                           + node["parameters"]["jsCode"] + "\n}\n")
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as handle:
                    handle.write(wrapped)
                    path = handle.name
                try:
                    proc = subprocess.run([NODE, "--check", path],
                                          capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                finally:
                    os.unlink(path)


class TestSafetyModel(unittest.TestCase):
    """These assertions are the product, not paperwork."""

    def test_there_is_exactly_one_http_node(self):
        http = [n for n in NODES if n["type"] == "n8n-nodes-base.httpRequest"]
        self.assertEqual(len(http), 1,
                         "a second HTTP node is how a read-only tool stops being one")

    def test_the_only_http_node_targets_the_official_api(self):
        node = next(n for n in NODES if n["type"] == "n8n-nodes-base.httpRequest")
        self.assertEqual(node["parameters"]["url"], "https://api.upwork.com/graphql")
        self.assertEqual(node["parameters"]["genericAuthType"], "oAuth2Api")

    def test_the_graphql_body_is_a_query_not_a_mutation(self):
        node = next(n for n in NODES if n["type"] == "n8n-nodes-base.httpRequest")
        body = node["parameters"]["jsonBody"]
        self.assertIn("marketplaceJobPostingsSearch", body)
        self.assertNotIn("mutation", body.lower())

    def test_no_node_calls_an_upwork_write_operation(self):
        blob = json.dumps(WORKFLOW).lower()
        for forbidden in ("submitproposal", "createproposal", "sendmessage",
                          "applyto", "createoffer", "mutation "):
            self.assertNotIn(forbidden, blob,
                             "found a write-shaped call: " + forbidden)

    def test_nothing_browser_automates_upwork(self):
        blob = json.dumps(WORKFLOW).lower()
        for forbidden in ("puppeteer", "playwright", "selenium", "webdriver",
                          "www.upwork.com/ab/", "headless"):
            self.assertNotIn(forbidden, blob,
                             "browser automation of Upwork is a ban vector: " + forbidden)

    def test_the_only_outbound_side_effect_is_slack(self):
        outbound = {n["type"] for n in NODES} - {
            "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.filter",
            "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.httpRequest",
        }
        self.assertEqual(outbound, {"n8n-nodes-base.slack"})

    def test_all_three_caps_are_configured_in_the_workflow(self):
        config = next(n for n in NODES if n["name"] == "Load Config")
        code = config["parameters"]["jsCode"]
        for cap in ("max_steps_per_run", "wall_clock_seconds", "cap_usd"):
            self.assertIn(cap, code)
        for env in ("RADAR_MAX_STEPS_PER_RUN", "RADAR_WALL_CLOCK_SECONDS",
                    "RADAR_SPEND_CAP_USD", "RADAR_BREAKER_CONSECUTIVE_ERRORS"):
            self.assertIn(env, code, "caps must come from config, not a literal")

    def test_something_actually_records_errors(self):
        """A breaker whose counter is never incremented is decoration.

        Exactly one node calls recordError(), and it is the node the HTTP
        error output routes to.
        """
        # `guard.recordError(` is the call site; every node embedding caps.js
        # also contains the method *definition*, which is not the same thing.
        callers = [n["name"] for n in CODE_NODES
                   if "guard.recordError(" in n["parameters"]["jsCode"]]
        self.assertEqual(callers, ["Build Breaker Alert"])
        http_error_branch = WORKFLOW["connections"]["Upwork GraphQL Search"]["main"][1]
        self.assertEqual([l["node"] for l in http_error_branch],
                         ["Build Breaker Alert"])

    def test_a_cap_refusal_does_not_inflate_the_error_streak(self):
        node = next(n for n in NODES if n["name"] == "Build Breaker Alert")
        code = node["parameters"]["jsCode"]
        self.assertIn("wasRefusedByCaps", code)
        self.assertIn("if (!wasRefusedByCaps)", code)

    def test_the_http_node_routes_errors_instead_of_swallowing_them(self):
        node = next(n for n in NODES if n["type"] == "n8n-nodes-base.httpRequest")
        self.assertEqual(node["onError"], "continueErrorOutput")

    def test_the_breaker_has_an_alert_path(self):
        self.assertIn("Post Breaker Alert", NAMES)
        targets = [l["node"] for b in WORKFLOW["connections"]["Caps OK?"]["main"]
                   for l in b]
        self.assertIn("Build Breaker Alert", targets)

    def test_no_credential_values_are_committed(self):
        for node in NODES:
            for cred in (node.get("credentials") or {}).values():
                with self.subTest(node=node["name"]):
                    self.assertEqual(cred["id"], "REPLACE_ME")

    def test_no_secret_shaped_strings_in_the_file(self):
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as handle:
            raw = handle.read()
        for marker in ("xoxb-", "xoxp-", "sk-", "-----BEGIN", "Bearer ey"):
            self.assertNotIn(marker, raw)


if __name__ == "__main__":
    unittest.main()
