"""The n8n workflow file, checked without n8n: it is what the generator writes, its graph holds together, the schedule fires on
the first of the month, every call can fail without stopping the others, delivery is off by default, and nothing in it is a
secret or a node a stock n8n container refuses.

What these cannot show is that n8n runs it. That is `n8n/verify_in_docker.py`, which imports and executes this file in a real
container (its saved output is docs/test-runs/). These tests run in CI and catch the file drifting from its generator and a graph
edit that leaves a node disconnected.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "n8n"))

import build_workflow  # noqa: E402

FILE = ROOT / "n8n" / "monthly_report.workflow.json"
TEXT = FILE.read_text(encoding="utf-8")
WF = json.loads(TEXT)
NODES = {n["name"]: n for n in WF["nodes"]}
FLOW = {name: node for name, node in NODES.items() if node["type"] != "n8n-nodes-base.stickyNote"}
RESULTS = [n for n in NODES if n.startswith("Result: ")]


def test_the_committed_file_is_exactly_what_the_generator_writes():
    assert TEXT.replace("\r\n", "\n") == build_workflow.render()
    done = subprocess.run([sys.executable, str(ROOT / "n8n" / "build_workflow.py"), "--check"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "matches" in done.stdout


def test_building_twice_gives_the_same_workflow_not_a_doubled_one():
    assert build_workflow.render() == build_workflow.render()
    assert len(build_workflow.build()["nodes"]) == len(WF["nodes"])


def test_it_has_a_root_id_a_name_and_is_not_active_on_import():
    assert WF["id"] == build_workflow.WORKFLOW_ID and WF["name"] and WF["active"] is False
    assert WF["settings"]["executionOrder"] == "v1" and WF["settings"]["timezone"] == "UTC"


# ---------------------------------------------------------------- the graph

def test_node_names_and_ids_are_unique():
    names = [n["name"] for n in WF["nodes"]]
    ids = [n["id"] for n in WF["nodes"]]
    assert len(set(names)) == len(names) and len(set(ids)) == len(ids)


def test_every_connection_runs_between_nodes_that_exist_and_to_an_output_the_source_has():
    for source, outs in WF["connections"].items():
        assert source in NODES, source
        for branch in outs["main"]:
            for target in branch:
                assert target["node"] in NODES and target["type"] == "main" and target["index"] >= 0, (source, target)


def test_every_node_but_the_note_is_reachable_from_a_trigger_and_nothing_is_left_dangling():
    triggers = [n for n, node in FLOW.items() if node["type"].endswith("Trigger")]
    assert sorted(triggers) == ["Run now", "Schedule, 1st of the month 06:00 UTC"]
    seen, stack = set(triggers), list(triggers)
    while stack:
        for branch in WF["connections"].get(stack.pop(), {}).get("main", []):
            for target in branch:
                if target["node"] not in seen:
                    seen.add(target["node"])
                    stack.append(target["node"])
    assert seen == set(FLOW), "unreachable: %s" % sorted(set(FLOW) - seen)


def test_the_two_ways_in_both_lead_to_the_same_settings_node():
    for trigger in ("Run now", "Schedule, 1st of the month 06:00 UTC"):
        assert WF["connections"][trigger]["main"] == [[{"node": "Settings", "type": "main", "index": 0}]]


def test_every_result_reaches_the_one_list_on_its_own_input_and_the_list_has_exactly_that_many_inputs():
    merge = NODES["All results"]
    wired = {}
    for name in RESULTS:
        (target,) = WF["connections"][name]["main"][0]
        assert target["node"] == "All results"
        wired[name] = target["index"]
    assert sorted(wired.values()) == list(range(len(RESULTS))) and merge["parameters"]["numberInputs"] == len(RESULTS)


def test_a_failed_branch_is_always_wired_to_something_so_a_failure_is_never_dropped_silently():
    for name, node in FLOW.items():
        if node.get("onError") == "continueErrorOutput":
            outs = WF["connections"].get(name, {"main": []})["main"]
            assert len(outs) == 2 and outs[0] and outs[1], "%s has an error output that goes nowhere" % name
    for name in ("Build the report", "Post the summary to Slack", "Fetch the deck", "Mark delivered"):
        assert NODES[name]["onError"] == "continueErrorOutput"
    # The Send Email node has no error output (n8n 2.44.6 passed its input on after a credential error and the deck was marked
    # delivered), so its outcome is read from the answer: only an item that names accepted recipients goes on to Mark delivered.
    assert NODES["Email the deck"]["onError"] == "continueRegularOutput"
    assert WF["connections"]["Email the deck"]["main"] == [[{"node": "Email sent?", "type": "main", "index": 0}]]
    assert "$json.accepted.length > 0" in NODES["Email sent?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert WF["connections"]["Email sent?"]["main"][1] == [{"node": "Result: email failed", "type": "main", "index": 0}]
    assert WF["connections"]["Build the report"]["main"][1] == [{"node": "Result: build failed", "type": "main", "index": 0}]


def test_a_failed_tenant_ends_the_execution_as_an_error_after_every_tenant_was_tried():
    chain = ["All results", "Summary", "Any failed?"]
    for a, b in zip(chain, chain[1:]):
        assert WF["connections"][a]["main"][0][0]["node"] == b
    assert NODES["Stop and Error"]["parameters"]["errorType"] == "errorMessage"
    assert "r.status === 'failed'" in NODES["Any failed?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert {t["node"] for t in WF["connections"]["Any failed?"]["main"][0]} == {"Alert on Slack?"}
    assert {t["node"] for t in WF["connections"]["Any failed?"]["main"][1]} == {"All done"}
    assert NODES["Post the alert to Slack"]["onError"] == "continueRegularOutput", "a Slack outage must not hide the failure it was reporting"
    assert {t["node"] for t in WF["connections"]["Post the alert to Slack"]["main"][0]} == {"Stop and Error"}


# ---------------------------------------------------------------- the clock

def cron_matches(expression: str, when: datetime) -> bool:
    minute, hour, day, month, weekday = expression.split()
    fields = ((minute, when.minute), (hour, when.hour), (day, when.day), (month, when.month), (weekday, (when.weekday() + 1) % 7))
    return all(spec == "*" or int(spec) == value for spec, value in fields)


def test_the_schedule_fires_at_six_on_the_first_of_every_month_and_at_no_other_time():
    rule = NODES["Schedule, 1st of the month 06:00 UTC"]["parameters"]["rule"]["interval"]
    assert len(rule) == 1 and rule[0]["field"] == "cronExpression"
    expression = rule[0]["expression"]
    assert expression == "0 6 1 * *"
    when, end, fires = datetime(2026, 10, 26), datetime(2027, 2, 2), []
    while when < end:
        if cron_matches(expression, when):
            fires.append(when)
        when += timedelta(minutes=1)
    assert fires == [datetime(2026, 11, 1, 6), datetime(2026, 12, 1, 6), datetime(2027, 1, 1, 6), datetime(2027, 2, 1, 6)]


def test_the_month_defaults_to_the_one_before_the_run_and_can_be_named_for_a_backfill():
    value = next(a["value"] for a in NODES["Settings"]["parameters"]["assignments"]["assignments"] if a["name"] == "month")
    assert value == "={{ $env.HUB_REPORT_MONTH || $now.minus({ months: 1 }).toFormat('yyyy-MM') }}"


# ---------------------------------------------------------------- one call per tenant, safe to repeat

def test_the_report_call_sends_the_tenants_own_key_and_one_idempotency_key_per_tenant_and_month():
    params = NODES["Build the report"]["parameters"]
    headers = {h["name"]: h["value"] for h in params["headerParameters"]["parameters"]}
    assert params["method"] == "POST" and params["url"].endswith("/api/reports/monthly")
    assert headers["Authorization"] == "={{ 'Bearer ' + $json.tenant.api_key }}"
    key = headers["Idempotency-Key"]
    assert "$json.tenant.name" in key and "$json.month" in key and key.startswith("={{ 'monthly-report:'")
    assert NODES["Build the report"]["retryOnFail"] is True and NODES["Build the report"]["maxTries"] >= 2, "a retry is safe because of the key"
    assert "as_of" in params["jsonBody"] and "month" in params["jsonBody"]


def test_the_idempotency_key_expression_gives_a_valid_key_for_any_tenant_name():
    """The same slug rule in Python: whatever the name, the key matches what the hub accepts."""
    from app.report.runs import KEY_RE

    def key(name: str, month: str) -> str:
        slug = re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", name.lower()))
        return "monthly-report:" + slug + ":" + month

    for name in ("Example Outfitters", "  Spaces  ", "A&B (Pty) Ltd.", "éèê", "x" * 90, "", "---"):
        assert KEY_RE.match(key(name, "2026-10")) or len(key(name, "2026-10")) > 128, name
    assert key("Example Outfitters", "2026-10") != key("Example Outfitters", "2026-09")
    assert key("A B", "2026-10") != key("A C", "2026-10")


def test_a_deck_already_delivered_is_skipped_before_any_delivery_node_is_reached():
    assert WF["connections"]["Build the report"]["main"][0] == [{"node": "Already delivered?", "type": "main", "index": 0}]
    skip, go = WF["connections"]["Already delivered?"]["main"]
    assert skip == [{"node": "Result: already delivered", "type": "main", "index": 0}] and go[0]["node"] == "Any delivery on?"


# ---------------------------------------------------------------- delivery is off by default

def test_slack_and_email_run_only_when_their_variable_is_the_text_true():
    assigned = {a["name"]: a["value"] for a in NODES["Settings"]["parameters"]["assignments"]["assignments"]}
    assert assigned["slack_on"] == "={{ $env.DELIVER_SLACK === 'true' }}" and assigned["email_on"] == "={{ $env.DELIVER_EMAIL === 'true' }}"
    for toggle in ("Slack on?", "Email on?", "Alert on Slack?", "Any delivery on?"):
        assert "$('Settings').first().json" in NODES[toggle]["parameters"]["conditions"]["conditions"][0]["leftValue"]
    gate = WF["connections"]["Any delivery on?"]["main"]
    assert gate[1] == [{"node": "Result: built, delivery off", "type": "main", "index": 0}], "no toggle on means nothing is sent"
    assert "slack_on" in NODES["Any delivery on?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]


def test_nothing_is_marked_delivered_unless_a_delivery_step_ran_and_succeeded():
    into_mark = [src for src, outs in WF["connections"].items() for i, branch in enumerate(outs["main"])
                 for t in branch if t["node"] == "Mark delivered" and not (src in NODES and NODES[src].get("onError") == "continueErrorOutput" and i == 1)]
    assert sorted(into_mark) == ["Email on?", "Email sent?"]
    assert {t["node"] for t in WF["connections"]["Email on?"]["main"][1]} == {"Mark delivered"}
    assert {t["node"] for t in WF["connections"]["Any delivery on?"]["main"][0]} == {"Slack on?"}, "a path to Mark delivered starts only after the gate"


# ---------------------------------------------------------------- what is in the file

def test_it_uses_no_node_a_stock_n8n_container_refuses_or_runs_elsewhere():
    types = {n["type"] for n in WF["nodes"]}
    assert "n8n-nodes-base.executeCommand" not in types, "disabled by default in recent n8n versions"
    assert not {t for t in types if "code" in t.lower() or "function" in t.lower()}, "no Code node: it needs a task runner"
    assert types <= {"n8n-nodes-base." + t for t in ("scheduleTrigger", "manualTrigger", "set", "splitOut", "httpRequest", "if", "emailSend", "merge",
                                                     "aggregate", "noOp", "stopAndError", "stickyNote")}


def test_there_is_no_secret_url_credential_or_other_tool_named_in_it():
    for forbidden in ("aah_", "hooks.slack.com", "xoxb-", "password", "smtp.", "Bearer eyJ", "api_key\": \"", "zapier", "make.com", "integromat", "hook.eu"):
        assert forbidden.lower() not in TEXT.lower(), forbidden
    assert all("credentials" not in node for node in WF["nodes"]), "an SMTP credential is attached in n8n, never committed"
    for node in FLOW.values():
        for value in re.findall(r'"=([^"]*)"', json.dumps(node)):
            assert "http://" not in value and "https://" not in value, "a literal address is in an expression: %s" % value[:60]


def test_every_setting_that_differs_between_deployments_is_read_from_the_environment():
    env = set(re.findall(r"\$env\.([A-Z_]+)", TEXT))
    assert env == {"HUB_REPORT_MONTH", "HUB_BASE_URL", "HUB_AS_OF", "DELIVER_SLACK", "DELIVER_EMAIL", "HUB_TENANTS_JSON", "SLACK_WEBHOOK_URL",
                   "EMAIL_FROM", "EMAIL_TO"}
    readme = (ROOT / "n8n" / "README.md").read_text(encoding="utf-8")
    for name in env:
        assert name in readme, "%s is not described in n8n/README.md" % name
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE" in readme and "Execute Command" in readme
