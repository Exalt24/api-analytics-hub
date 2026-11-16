# LIFESPAN: permanent
"""Write the two importable n8n workflows of the job pipeline.

    python n8n/build_job_workflow.py            # rewrites job_ingest.workflow.json and job_apply.workflow.json beside this file
    python n8n/build_job_workflow.py --check    # exit 1 when a committed file is not what this script writes

JOB INGEST (`job_ingest.workflow.json`). An hourly Schedule trigger (and a Run now trigger) reads the vendor list from `JOB_VENDORS_JSON`, makes one item
per vendor, and a Code node pulls each vendor (n8n/jobs/pull.js) and normalises the answer (n8n/jobs/normalize.js); both files are inlined into the
node by this script, so the node cannot drift from the tested files. The batch id is `<vendor>-<UTC hour>`, so a rerun inside the same hour is the hub's
replay and writes nothing twice. Each batch goes to `POST /api/jobs/ingest/<vendor>` on the hub, which archives the raw JSON, dedupes in Redis, upserts
into Postgres and embeds into Qdrant. A vendor that fails goes down its own error path and never stops the others; the run ends as an error that names
each failed vendor. Two Webhook triggers take pushes (Bright Data's `endpoint` delivery, and any delivery vendor such as Grepsr) through the same
normalise and ingest steps, and answer 200 before the work starts, as Bright Data's page asks.

JOB APPLY (`job_apply.workflow.json`). A Webhook receives `{apply_url, job_id}`, starts a run on the hub with this execution's resume URL, and a Wait node
holds the execution until the hub calls that URL, which it does when the run reaches submitted, failed or manual. The hub's run is the state; n8n holds
the wait, then reads the final state and ends the execution as done, manual review or an error.

Both need `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and the variables in n8n/README.md (section Job pipeline).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
INGEST_OUT = HERE / "job_ingest.workflow.json"
APPLY_OUT = HERE / "job_apply.workflow.json"
INGEST_ID = "hubJobIngest0001"
APPLY_ID = "hubJobApply00001"

SETTINGS = "$('Settings').first().json"


class Flow:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.conn: dict = {}

    def node(self, name: str, type_: str, version: float, x: int, y: int, parameters: dict, **extra) -> str:
        nid = "%02d%s" % (len(self.nodes) + 1, "0e3b1a-5c7d-4e2f-9a10-%012x" % (0x6a6f6270 + len(self.nodes)))
        self.nodes.append({"parameters": parameters, "id": nid, "name": name, "type": type_, "typeVersion": version, "position": [x, y], **extra})
        return name

    def link(self, src: str, dst: str, out: int = 0, outputs: int = 1) -> None:
        c = self.conn.setdefault(src, {"main": [[] for _ in range(outputs)]})
        while len(c["main"]) <= out:
            c["main"].append([])
        c["main"][out].append({"node": dst, "type": "main", "index": 0})

    def render(self, wid: str, name: str) -> dict:
        return {"id": wid, "name": name, "active": False, "nodes": self.nodes, "connections": self.conn,
                "settings": {"executionOrder": "v1", "timezone": "UTC", "saveManualExecutions": True}, "pinData": {}, "meta": {"templateCredsSetupCompleted": False}}


def assignment(name: str, kind: str, value) -> dict:
    return {"id": "a-" + name, "name": name, "type": kind, "value": value}


def set_node(f: Flow, name: str, x: int, y: int, fields: list[dict]) -> str:
    return f.node(name, "n8n-nodes-base.set", 3.4, x, y, {"assignments": {"assignments": fields}, "options": {}})


def http(f: Flow, name: str, x: int, y: int, method: str, url: str, headers: list[tuple[str, str]], body: str | None = None, *, retries: int = 0,
         on_error: str = "continueErrorOutput") -> str:
    params: dict = {"method": method, "url": "=" + url, "options": {"timeout": 120000}}
    if headers:
        params["sendHeaders"] = True
        params["headerParameters"] = {"parameters": [{"name": n, "value": "=" + v} for n, v in headers]}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": "=" + body})
    extra: dict = {"onError": on_error}
    if retries:
        extra.update({"retryOnFail": True, "maxTries": retries, "waitBetweenTries": 3000})
    return f.node(name, "n8n-nodes-base.httpRequest", 4.2, x, y, params, **extra)


def read(name: str) -> str:
    return (HERE / "jobs" / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def strip_exports(src: str) -> str:
    """The files end with `if (typeof module ...) { module.exports = ... }`, which a Code node does not need and must not run."""
    return src.split("if (typeof module !== 'undefined'")[0]


PULL_CODE = (
    "// Inlined from n8n/jobs/normalize.js and n8n/jobs/pull.js by build_job_workflow.py. Edit those files, not this node.\n"
    + strip_exports(read("normalize.js")) + "\n" + strip_exports(read("pull.js")) + "\n"
    + """
const http = async (o) => {
  const res = await this.helpers.httpRequest({ method: o.method || 'GET', url: o.url, headers: o.headers, qs: o.qs, body: o.body, json: true,
    returnFullResponse: true, ignoreHttpStatusErrors: true });
  const headers = {};
  for (const [k, v] of Object.entries(res.headers || {})) headers[k.toLowerCase()] = Array.isArray(v) ? v[0] : v;
  return { status: res.statusCode, headers, body: res.body };
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hour = new Date().toISOString().slice(0, 13).replace(/[-T:]/g, '');
const out = [];
for (const item of $input.all()) {
  const { vendor, cfg } = item.json;
  try {
    const body = await pullVendor(vendor, cfg, http, sleep);
    const n = normalizeBatch(vendor, body);
    out.push({ json: { vendor, batch_id: vendor + '-' + hour, raw: body, jobs: n.jobs, invalid: n.invalid + (n.received - n.jobs.length - n.invalid), received: n.received } });
  } catch (e) {
    out.push({ json: { vendor, error: String(e.message || e) } });
  }
}
return out;
"""
)

PUSH_CODE = (
    "// Inlined from n8n/jobs/normalize.js by build_job_workflow.py.\n" + strip_exports(read("normalize.js")) + "\n"
    + """
const input = $input.first().json;
const vendor = input.vendor;
let payload = input.body;
if (typeof payload === 'string') { try { payload = JSON.parse(payload); } catch (e) { payload = []; } }
if (!Array.isArray(payload) && payload && Array.isArray(payload.items)) payload = payload.items;
const map = input.map || undefined;
const n = normalizeBatch(vendor, payload, { map });
const batch = (input.headers && (input.headers['dca-collection-id'] || input.headers['x-batch-id'])) || (vendor + '-push-' + new Date().toISOString().slice(0, 16).replace(/[-T:]/g, ''));
return [{ json: { vendor, batch_id: String(batch), raw: payload, jobs: n.jobs, invalid: n.invalid, received: n.received } }];
"""
)


def ingest() -> dict:
    f = Flow()
    f.node("Notes", "n8n-nodes-base.stickyNote", 1, -420, -300, {
        "content": ("## Job ingest\nEvery hour: pull each vendor in `JOB_VENDORS_JSON`, normalise, and send the batch to the hub, which archives the raw JSON, dedupes in "
                    "Redis, upserts into Postgres and embeds into Qdrant. A rerun in the same hour is the hub's replay. Two webhooks take pushes.\n"
                    "**Needs** `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and the variables in n8n/README.md."), "height": 200, "width": 620, "color": 4})
    f.node("Schedule, every hour", "n8n-nodes-base.scheduleTrigger", 1.2, -420, 0, {"rule": {"interval": [{"field": "cronExpression", "expression": "0 * * * *"}]}})
    f.node("Run now", "n8n-nodes-base.manualTrigger", 1, -420, 160, {})
    set_node(f, "Settings", -160, 80, [
        assignment("hub_url", "string", "={{ ($env.HUB_BASE_URL || '').replace(/\\/+$/, '') }}"),
        assignment("hub_key", "string", "={{ $env.HUB_API_KEY }}"),
        assignment("vendors", "array", "={{ JSON.parse($env.JOB_VENDORS_JSON) }}")])
    f.node("One item per vendor", "n8n-nodes-base.splitOut", 1, 100, 80, {"fieldToSplitOut": "vendors", "include": "allOtherFields", "options": {"destinationFieldName": "v"}})
    set_node(f, "Vendor and settings", 340, 80, [
        assignment("vendor", "string", "={{ $json.v.vendor }}"), assignment("cfg", "object", "={{ $json.v.cfg }}")])
    f.node("Pull and normalise", "n8n-nodes-base.code", 2, 580, 80, {"language": "javaScript", "mode": "runOnceForAllItems", "jsCode": PULL_CODE})
    f.node("Pull failed?", "n8n-nodes-base.if", 2.2, 820, 80, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c-pull-failed", "leftValue": "={{ $json.error }}", "rightValue": "", "operator": {"type": "string", "operation": "exists", "singleValue": True}}],
        "combinator": "and"}, "options": {}})
    http(f, "Send the batch to the hub", 1080, 160, "POST", "{{ " + SETTINGS + ".hub_url }}/api/jobs/ingest/{{ $json.vendor }}",
         [("Authorization", "{{ 'Bearer ' + " + SETTINGS + ".hub_key }}"), ("Content-Type", "application/json")],
         "{{ JSON.stringify({ batch_id: $json.batch_id, raw: $json.raw, jobs: $json.jobs, invalid: $json.invalid, trigger: 'schedule' }) }}", retries=3)
    set_node(f, "Result: ingested", 1340, 120, [
        assignment("vendor", "string", "={{ $('Pull failed?').item.json.vendor }}"), assignment("status", "string", "ingested"),
        assignment("detail", "string", "={{ 'new ' + $json.new_jobs + ', duplicates ' + $json.duplicates + ', embedded ' + $json.embedded + ', status ' + $json.status + ($json.replayed ? ', replay' : '') }}")])
    set_node(f, "Result: hub refused", 1340, 260, [
        assignment("vendor", "string", "={{ $('Pull failed?').item.json.vendor }}"), assignment("status", "string", "failed"),
        assignment("detail", "string", "={{ 'the hub refused the batch: ' + JSON.stringify($json.error || $json).slice(0, 200) }}")])
    set_node(f, "Result: pull failed", 1080, -60, [
        assignment("vendor", "string", "={{ $json.vendor }}"), assignment("status", "string", "failed"), assignment("detail", "string", "={{ 'the pull failed: ' + $json.error }}")])
    f.node("All results", "n8n-nodes-base.merge", 3, 1600, 120, {"numberInputs": 3})
    f.node("Summary", "n8n-nodes-base.aggregate", 1, 1840, 120, {"aggregate": "aggregateAllItemData", "destinationFieldName": "results", "options": {}})
    f.node("Any failed?", "n8n-nodes-base.if", 2.2, 2080, 120, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c-any-failed", "leftValue": "={{ $json.results.filter(r => r.status === 'failed').length > 0 }}", "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}})
    f.node("Stop and Error", "n8n-nodes-base.stopAndError", 1, 2340, 60, {"errorMessage": "={{ 'Job ingest: ' + $json.results.filter(r => r.status === 'failed').map(r => r.vendor + ' (' + r.detail + ')').join('; ') }}"})
    set_node(f, "All done", 2340, 200, [assignment("summary", "string", "={{ $json.results.map(r => r.vendor + ': ' + r.detail).join(' | ') }}")])

    for t in ("Schedule, every hour", "Run now"):
        f.link(t, "Settings")
    f.link("Settings", "One item per vendor")
    f.link("One item per vendor", "Vendor and settings")
    f.link("Vendor and settings", "Pull and normalise")
    f.link("Pull and normalise", "Pull failed?")
    f.link("Pull failed?", "Result: pull failed", 0, 2)
    f.link("Pull failed?", "Send the batch to the hub", 1, 2)
    f.link("Send the batch to the hub", "Result: ingested", 0, 2)
    f.link("Send the batch to the hub", "Result: hub refused", 1, 2)
    for i, n in enumerate(("Result: pull failed", "Result: ingested", "Result: hub refused")):
        c = f.conn.setdefault(n, {"main": [[]]})
        c["main"][0].append({"node": "All results", "type": "main", "index": i})
    f.link("All results", "Summary")
    f.link("Summary", "Any failed?")
    f.link("Any failed?", "Stop and Error", 0, 2)
    f.link("Any failed?", "All done", 1, 2)

    # the push side: one webhook for Bright Data deliveries and one for any other delivery vendor, 200 at once, then normalise and ingest
    for i, (vendor, path) in enumerate((("brightdata", "jobs-push/brightdata"), ("grepsr", "jobs-push/grepsr"))):
        y = 520 + i * 260
        f.node("Webhook: %s" % vendor, "n8n-nodes-base.webhook", 2, -420, y, {"httpMethod": "POST", "path": path, "responseMode": "onReceived", "options": {"rawBody": False}},
               webhookId="job-push-%s" % vendor)
        set_node(f, "Delivery: %s" % vendor, -160, y, [
            assignment("vendor", "string", vendor), assignment("body", "object" if vendor == "grepsr" else "array", "={{ $json.body }}"),
            assignment("headers", "object", "={{ $json.headers }}"), assignment("map", "object", "={{ $env.GREPSR_MAP_JSON ? JSON.parse($env.GREPSR_MAP_JSON) : {} }}" if vendor == "grepsr" else "={{ {} }}")])
        f.node("Normalise: %s" % vendor, "n8n-nodes-base.code", 2, 100, y, {"language": "javaScript", "mode": "runOnceForAllItems", "jsCode": PUSH_CODE})
        http(f, "Ingest: %s" % vendor, 360, y, "POST", "{{ ($env.HUB_BASE_URL || '').replace(/\\/+$/, '') }}/api/jobs/ingest/{{ $json.vendor }}",
             [("Authorization", "{{ 'Bearer ' + $env.HUB_API_KEY }}"), ("Content-Type", "application/json")],
             "{{ JSON.stringify({ batch_id: $json.batch_id, raw: $json.raw, jobs: $json.jobs, invalid: $json.invalid, trigger: 'webhook' }) }}", retries=3)
        f.link("Webhook: %s" % vendor, "Delivery: %s" % vendor)
        f.link("Delivery: %s" % vendor, "Normalise: %s" % vendor)
        f.link("Normalise: %s" % vendor, "Ingest: %s" % vendor)
    return f.render(INGEST_ID, "Job ingest")


def apply_flow() -> dict:
    f = Flow()
    f.node("Notes", "n8n-nodes-base.stickyNote", 1, -420, -240, {
        "content": ("## Apply with review\nPOST `{apply_url, job_id}` to this workflow's webhook. The hub starts the run and this execution waits at the Wait node until the hub "
                    "calls its resume URL (the run reached submitted, failed or manual review). The Wait holds no browser: the run state lives in the hub."), "height": 160, "width": 620, "color": 4})
    f.node("Webhook: apply", "n8n-nodes-base.webhook", 2, -420, 0, {"httpMethod": "POST", "path": "jobs-apply", "responseMode": "onReceived", "options": {}}, webhookId="job-apply-start")
    hub = "{{ ($env.HUB_BASE_URL || '').replace(/\\/+$/, '') }}"
    auth = ("Authorization", "{{ 'Bearer ' + $env.HUB_API_KEY }}")
    http(f, "Start the run on the hub", -160, 0, "POST", hub + "/api/jobs/apply", [auth, ("Content-Type", "application/json")],
         "{{ JSON.stringify({ apply_url: $json.body.apply_url, job_id: $json.body.job_id || null, resume_url: $execution.resumeUrl }) }}", retries=2)
    f.node("Wait for the run to finish", "n8n-nodes-base.wait", 1.1, 120, 0, {"resume": "webhook", "httpMethod": "POST", "options": {}}, webhookId="job-apply-resume")
    http(f, "Read the run", 380, 0, "GET", hub + "/api/jobs/runs/{{ $('Start the run on the hub').item.json.run_id }}", [auth])
    f.node("Run submitted?", "n8n-nodes-base.if", 2.2, 640, 0, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c-submitted", "leftValue": "={{ $json.state }}", "rightValue": "submitted", "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"}, "options": {}})
    set_node(f, "Done: submitted", 900, -80, [assignment("run_id", "string", "={{ $json.id }}"), assignment("outcome", "string", "submitted")])
    f.node("Manual review?", "n8n-nodes-base.if", 2.2, 900, 100, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c-manual", "leftValue": "={{ $json.state }}", "rightValue": "manual", "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"}, "options": {}})
    set_node(f, "Done: manual review", 1160, 40, [assignment("run_id", "string", "={{ $json.id }}"), assignment("outcome", "string", "manual")])
    f.node("Stop and Error", "n8n-nodes-base.stopAndError", 1, 1160, 180, {"errorMessage": "={{ 'The application run ended as ' + $json.state + ': ' + ($json.detail || 'no detail') }}"})
    http(f, "Report the outcome", 1420, -20, "POST", "{{ $env.APPLY_REPORT_URL || ($env.HUB_BASE_URL || '') + '/health' }}", [("Content-Type", "application/json")],
         "{{ JSON.stringify({ run_id: $json.run_id, outcome: $json.outcome }) }}", on_error="continueRegularOutput")
    f.link("Webhook: apply", "Start the run on the hub")
    f.link("Start the run on the hub", "Wait for the run to finish", 0, 2)
    f.link("Wait for the run to finish", "Read the run")
    f.link("Read the run", "Run submitted?")
    f.link("Run submitted?", "Done: submitted", 0, 2)
    f.link("Run submitted?", "Manual review?", 1, 2)
    f.link("Manual review?", "Done: manual review", 0, 2)
    f.link("Manual review?", "Stop and Error", 1, 2)
    f.link("Done: submitted", "Report the outcome")
    f.link("Done: manual review", "Report the outcome")
    return f.render(APPLY_ID, "Job apply with review")


def render() -> dict[Path, str]:
    return {INGEST_OUT: json.dumps(ingest(), indent=2, ensure_ascii=False) + "\n", APPLY_OUT: json.dumps(apply_flow(), indent=2, ensure_ascii=False) + "\n"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    files = render()
    if args.check:
        bad = [path.name for path, text in files.items() if not path.exists() or path.read_text(encoding="utf-8").replace("\r\n", "\n") != text]
        print("the committed workflows match" if not bad else "out of date: %s (run python n8n/build_job_workflow.py)" % ", ".join(bad))
        return 1 if bad else 0
    for path, text in files.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print("wrote %s" % path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
