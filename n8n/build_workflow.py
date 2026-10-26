# LIFESPAN: permanent
"""Write n8n/monthly_report.workflow.json, the importable n8n workflow that runs the monthly report for each tenant.

    python n8n/build_workflow.py            # rewrites the JSON beside this file
    python n8n/build_workflow.py --check    # exit 1 when the committed JSON is not what this script writes

The workflow is generated rather than hand edited so its connections cannot drift from its nodes (tests/test_n8n_workflow.py
checks that, and that the committed file is what this script writes). It uses no Code node and no Execute Command node: n8n
2.x disables the second by default and runs the first in a task runner, and both are things a stock container may not have.
Everything it does is an HTTP Request node calling the hub's own endpoints (app/api/reports.py), a Set, an IF or a Merge.
Settings come from environment variables, which n8n only lets expressions read when N8N_BLOCK_ENV_ACCESS_IN_NODE is false.

THE SHAPE, per tenant (n8n runs each node over every tenant before the next node, and a tenant that fails goes down the error
output of its node, so it never stops the others):

  Schedule (1st of the month, 06:00 UTC) or Run now -> Settings -> one item per tenant -> Build the report
    Build ok     -> already delivered?  yes: skip
                                        no:  any delivery switched on?  no: built, delivery off
                                                                        yes: Slack?  -> post it   (failure: result "failed")
                                                                             email?  -> fetch the deck -> send it -> was it accepted?
                                                                                        (no: "failed", and nothing is marked delivered)
                                                                             -> mark delivered -> result "delivered"
    Build failed -> result "failed"
  every result -> one list -> any failed?  yes: Slack alert if switched on -> Stop and Error (the execution shows as failed)
                                           no:  done

Delivery is OFF unless DELIVER_SLACK or DELIVER_EMAIL is the text `true`, so the workflow runs end to end with no credential
beyond the hub's own API keys. Nothing here is a Make scenario or a Zap: those would call the same HTTP endpoint, and none was built.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "monthly_report.workflow.json"
WORKFLOW_ID = "hubMonthlyReport01"
CRON = "0 6 1 * *"

TENANT = "$('One item per tenant').item.json.tenant"
SETTINGS = "$('Settings').first().json"
KEY_EXPR = ("'monthly-report:' + $json.tenant.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') + ':' + $json.month")

_nodes: list[dict] = []
_ids: dict[str, str] = {}


def node(name: str, type_: str, version: float, x: int, y: int, parameters: dict, **extra) -> str:
    _ids[name] = "%02d" % (len(_nodes) + 1) + "0e3b1a-5c7d-4e2f-9a10-" + ("%012x" % (0x4d6f6e74686c79 + len(_nodes)))
    _nodes.append({"parameters": parameters, "id": _ids[name], "name": name, "type": type_, "typeVersion": version,
                   "position": [x, y], **extra})
    return name


def assignment(name: str, kind: str, value) -> dict:
    return {"id": "a-" + name, "name": name, "type": kind, "value": value}


def set_node(name: str, x: int, y: int, fields: list[dict]) -> str:
    return node(name, "n8n-nodes-base.set", 3.4, x, y, {"assignments": {"assignments": fields}, "options": {}})


def if_node(name: str, x: int, y: int, left: str, kind: str = "boolean") -> str:
    """An IF on one true or false value. Output 0 is the true branch, output 1 the false."""
    return node(name, "n8n-nodes-base.if", 2.2, x, y, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c-" + name.lower().replace(" ", "-"), "leftValue": "={{ " + left + " }}", "rightValue": "",
                        "operator": {"type": kind, "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}})


def http(name: str, x: int, y: int, method: str, url: str, headers: list[tuple[str, str]], body: str | None = None,
         *, on_error: str = "continueErrorOutput", retries: int = 0, file: bool = False) -> str:
    params: dict = {"method": method, "url": "=" + url, "options": {"timeout": 120000}}
    if headers:
        params["sendHeaders"] = True
        params["headerParameters"] = {"parameters": [{"name": n, "value": "=" + v} for n, v in headers]}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": "=" + body})
    if file:
        params["options"]["response"] = {"response": {"responseFormat": "file"}}
    extra: dict = {"onError": on_error}
    if retries:
        extra.update({"retryOnFail": True, "maxTries": retries, "waitBetweenTries": 3000})
    return node(name, "n8n-nodes-base.httpRequest", 4.2, x, y, params, **extra)


def build() -> dict:
    _nodes.clear()
    _ids.clear()
    node("Notes", "n8n-nodes-base.stickyNote", 1, -400, -260, {
        "content": ("## Monthly report for each tenant\n"
                    "Runs on the 1st of the month at 06:00 UTC and asks the hub for last month's deck, once for each tenant in "
                    "`HUB_TENANTS_JSON`. Asking twice for a month gives the same deck and sends nothing twice.\n"
                    "**Needs** `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and the variables in n8n/README.md. "
                    "Slack and email are **off** until `DELIVER_SLACK` or `DELIVER_EMAIL` is `true`."),
        "height": 230, "width": 560, "color": 4})

    node("Schedule, 1st of the month 06:00 UTC", "n8n-nodes-base.scheduleTrigger", 1.2, -400, 0,
         {"rule": {"interval": [{"field": "cronExpression", "expression": CRON}]}})
    node("Run now", "n8n-nodes-base.manualTrigger", 1, -400, 160, {})

    set_node("Settings", -120, 80, [
        assignment("month", "string", "={{ $env.HUB_REPORT_MONTH || $now.minus({ months: 1 }).toFormat('yyyy-MM') }}"),
        assignment("hub_url", "string", "={{ ($env.HUB_BASE_URL || '').replace(/\\/+$/, '') }}"),
        assignment("as_of", "string", "={{ $env.HUB_AS_OF || '' }}"),
        assignment("slack_on", "boolean", "={{ $env.DELIVER_SLACK === 'true' }}"),
        assignment("email_on", "boolean", "={{ $env.DELIVER_EMAIL === 'true' }}"),
        assignment("tenants", "array", "={{ JSON.parse($env.HUB_TENANTS_JSON) }}"),
    ])
    node("One item per tenant", "n8n-nodes-base.splitOut", 1, 140, 80,
         {"fieldToSplitOut": "tenants", "include": "allOtherFields", "options": {"destinationFieldName": "tenant"}})

    http("Build the report", 400, 80, "POST", "{{ $json.hub_url }}/api/reports/monthly",
         [("Authorization", "{{ 'Bearer ' + $json.tenant.api_key }}"), ("Idempotency-Key", "{{ " + KEY_EXPR + " }}"),
          ("Content-Type", "application/json")],
         "{{ JSON.stringify($json.as_of ? { month: $json.month, as_of: $json.as_of } : { month: $json.month }) }}",
         retries=3)

    if_node("Already delivered?", 680, -40, "$json.delivered")
    set_node("Result: already delivered", 960, -160, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "={{ $json.month }}"),
        assignment("status", "string", "skipped, already delivered"),
        assignment("detail", "string", "={{ 'deck ' + $json.sha256.slice(0, 12) + ', sent before' }}")])

    if_node("Any delivery on?", 960, 0, SETTINGS + ".slack_on || " + SETTINGS + ".email_on")
    set_node("Result: built, delivery off", 1240, -40, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "={{ $json.month }}"),
        assignment("status", "string", "built, delivery off"),
        assignment("detail", "string", "={{ 'deck ' + $json.sha256.slice(0, 12) + ', ' + $json.slides + ' slides, nothing sent' }}")])

    if_node("Slack on?", 1240, 120, SETTINGS + ".slack_on")
    http("Post the summary to Slack", 1520, 80, "POST", "{{ $env.SLACK_WEBHOOK_URL }}", [("Content-Type", "application/json")],
         "{{ JSON.stringify({ text: 'Monthly report for ' + " + TENANT + ".name + ', ' + $json.month + ': ' + $json.slides + "
         "' slides from ' + $json.sources.join(', ') + '. ' + ($json.narrative[0] || '') }) }}")
    set_node("Result: Slack failed", 1800, 260, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "={{ $json.month }}"),
        assignment("status", "string", "failed"),
        assignment("detail", "string", "={{ 'the deck was built but the Slack post failed: ' + ($json.error && $json.error.message || 'unknown') }}")])

    if_node("Email on?", 1800, 120, SETTINGS + ".email_on")
    http("Fetch the deck", 2080, 60, "GET", "{{ " + SETTINGS + ".hub_url }}/api/reports/monthly/{{ " + SETTINGS + ".month }}/deck",
         [("Authorization", "{{ 'Bearer ' + " + TENANT + ".api_key }}")], file=True)
    node("Email the deck", "n8n-nodes-base.emailSend", 2.1, 2360, 60, {
        "fromEmail": "={{ $env.EMAIL_FROM }}", "toEmail": "={{ $env.EMAIL_TO }}",
        "subject": "={{ 'Monthly report, ' + " + SETTINGS + ".month + ', ' + " + TENANT + ".name }}",
        "emailFormat": "text", "text": "The monthly report is attached.", "options": {"attachments": "data"}},
         onError="continueRegularOutput")
    # The Send Email node has no error output: with a missing credential n8n 2.44.6 recorded the error and passed the input item on as
    # if the mail had gone, which marked the deck delivered. So its success is read from its answer: the mail server's reply names
    # the recipients it accepted, and an item with none is a failure.
    if_node("Email sent?", 2640, 60, "Array.isArray($json.accepted) && $json.accepted.length > 0")
    set_node("Result: email failed", 2920, 260, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "=" + "{{ " + SETTINGS + ".month }}"),
        assignment("status", "string", "failed"),
        assignment("detail", "string", "={{ 'the deck was built but the email was not sent: ' + ($json.error && ($json.error.description || $json.error.message) || 'the mail server accepted no recipient') }}")])

    http("Mark delivered", 2920, 0, "POST", "{{ " + SETTINGS + ".hub_url }}/api/reports/monthly/{{ " + SETTINGS + ".month }}/delivered",
         [("Authorization", "{{ 'Bearer ' + " + TENANT + ".api_key }}")], "{{ JSON.stringify({}) }}", retries=3)
    set_node("Result: delivered", 3200, -40, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "=" + "{{ " + SETTINGS + ".month }}"),
        assignment("status", "string", "delivered"), assignment("detail", "string", "marked delivered, so a second run sends nothing")])
    set_node("Result: not marked delivered", 3200, 140, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "=" + "{{ " + SETTINGS + ".month }}"),
        assignment("status", "string", "failed"),
        assignment("detail", "string", "={{ 'it was sent but could not be marked delivered: ' + ($json.error && $json.error.message || 'unknown') }}")])

    set_node("Result: build failed", 680, 300, [
        assignment("tenant", "string", "={{ " + TENANT + ".name }}"), assignment("month", "string", "=" + "{{ " + SETTINGS + ".month }}"),
        assignment("status", "string", "failed"),
        assignment("detail", "string", "={{ ($json.error && ($json.error.description || $json.error.message)) || 'unknown error' }}")])

    node("All results", "n8n-nodes-base.merge", 3.1, 3480, 80, {"numberInputs": 7})
    node("Summary", "n8n-nodes-base.aggregate", 1, 3720, 80, {"aggregate": "aggregateAllItemData", "destinationFieldName": "data", "options": {}})
    if_node("Any failed?", 3960, 80, "$json.data.some(r => r.status === 'failed')")
    node("All done", "n8n-nodes-base.noOp", 1, 4240, -20, {})
    if_node("Alert on Slack?", 4240, 180, SETTINGS + ".slack_on")
    http("Post the alert to Slack", 4520, 140, "POST", "{{ $env.SLACK_WEBHOOK_URL }}", [("Content-Type", "application/json")],
         "{{ JSON.stringify({ text: 'Monthly report FAILED for ' + $('Summary').first().json.data.filter(r => r.status === 'failed')"
         ".map(r => r.tenant + ' (' + r.detail + ')').join('; ') }) }}", on_error="continueRegularOutput")
    node("Stop and Error", "n8n-nodes-base.stopAndError", 1, 4800, 180, {
        "errorType": "errorMessage",
        "errorMessage": "={{ 'Monthly report failed for ' + $('Summary').first().json.data.filter(r => r.status === 'failed')"
                        ".map(r => r.tenant + ' (' + r.detail + ')').join('; ') }}"})

    c: dict[str, dict] = {}

    def link(src: str, dst: str, output: int = 0, outputs: int = 1) -> None:
        main = c.setdefault(src, {"main": [[] for _ in range(outputs)]})["main"]
        while len(main) <= output:
            main.append([])
        main[output].append({"node": dst, "type": "main", "index": 0})

    link("Schedule, 1st of the month 06:00 UTC", "Settings")
    link("Run now", "Settings")
    link("Settings", "One item per tenant")
    link("One item per tenant", "Build the report")
    link("Build the report", "Already delivered?", 0, 2)
    link("Build the report", "Result: build failed", 1, 2)
    link("Already delivered?", "Result: already delivered", 0, 2)
    link("Already delivered?", "Any delivery on?", 1, 2)
    link("Any delivery on?", "Slack on?", 0, 2)
    link("Any delivery on?", "Result: built, delivery off", 1, 2)
    link("Slack on?", "Post the summary to Slack", 0, 2)
    link("Slack on?", "Email on?", 1, 2)
    link("Post the summary to Slack", "Email on?", 0, 2)
    link("Post the summary to Slack", "Result: Slack failed", 1, 2)
    link("Email on?", "Fetch the deck", 0, 2)
    link("Email on?", "Mark delivered", 1, 2)
    link("Fetch the deck", "Email the deck", 0, 2)
    link("Fetch the deck", "Result: email failed", 1, 2)
    link("Email the deck", "Email sent?")
    link("Email sent?", "Mark delivered", 0, 2)
    link("Email sent?", "Result: email failed", 1, 2)
    link("Mark delivered", "Result: delivered", 0, 2)
    link("Mark delivered", "Result: not marked delivered", 1, 2)
    for i, name in enumerate(("Result: already delivered", "Result: built, delivery off", "Result: delivered", "Result: build failed",
                              "Result: Slack failed", "Result: email failed", "Result: not marked delivered")):
        c.setdefault(name, {"main": [[]]})["main"][0].append({"node": "All results", "type": "main", "index": i})
    link("All results", "Summary")
    link("Summary", "Any failed?")
    link("Any failed?", "Alert on Slack?", 0, 2)
    link("Any failed?", "All done", 1, 2)
    link("Alert on Slack?", "Post the alert to Slack", 0, 2)
    link("Alert on Slack?", "Stop and Error", 1, 2)
    link("Post the alert to Slack", "Stop and Error")

    return {
        "id": WORKFLOW_ID,
        "name": "Monthly report for each tenant",
        "active": False,
        "nodes": _nodes,
        "connections": c,
        "settings": {"executionOrder": "v1", "timezone": "UTC", "saveManualExecutions": True},
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": False},
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="exit 1 when the committed JSON differs from what this writes")
    args = p.parse_args(argv)
    text = render()
    if args.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8").replace("\r\n", "\n") == text
        print("the committed workflow matches" if same else "the committed workflow is out of date: run python n8n/build_workflow.py")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s: %d nodes" % (OUT.name, len(_nodes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
