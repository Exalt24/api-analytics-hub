# LIFESPAN: permanent
"""Really run the monthly report workflow in a throwaway n8n container, against the real API, on invented data.

    python n8n/verify_in_docker.py [--summary docs/test-runs/n8n-run.txt] [--image n8nio/n8n:2.44.6]

What it does, and tears down at the end (every container, database and server it starts):

  1. makes a scratch database on the Postgres named by ANALYTICS_TEST_DSN (default: the compose one on port 5433), applies every
     migration, and loads the invented sample tenant (a store, a website, ads and search, through their stand-in servers) plus a
     second store, a tenant with no store (its report cannot be built: 422) and a tenant whose API key is wrong (401);
  2. starts the hub API (uvicorn) on the loopback address against that database, and a stand-in Slack webhook that records what it
     is sent;
  3. runs the workflow in `n8nio/n8n` with `n8n import:workflow` and `n8n execute`, one throwaway container for each of six runs:
     delivery off; the same again (the same deck, built nothing, sent nothing); Slack on; Slack on again (no second message);
     only the tenants that work (the execution succeeds); email on without an SMTP credential (the delivery step fails and says so);
  4. reads the execution JSON that `n8n execute` prints, node by node: how many items left each output of each node, what the result
     nodes say, whether the report endpoint was called and what it answered, and whether the failure branch fired; checks the
     database and the Slack stand-in against what each run should have done; and writes a summary.

`status: success` and `finished: true` are not taken as proof on their own: the checks read the node outputs. Exit 0 when every
check holds, 1 when one does not. It needs Docker, the n8nio/n8n image, Postgres and the repository's Python environment. Nothing
here touches a real ad account, property, store, SMTP server or Slack workspace; the API keys it makes are random and die with the
scratch database, and the env file that carries them to the container is deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.report.demo_data import DEMO_RUNS, demo_points  # noqa: E402

import build_sample_report as sample  # noqa: E402
from build_workflow import WORKFLOW_ID, render  # noqa: E402

DEFAULT_DSN = "postgresql://analytics:analytics@localhost:5433/analytics"
MONTH = "2026-10"
AS_OF = sample.AS_OF.isoformat()
DOCKER_TIMEOUT = 300
#: When set (--raw-dir), what `n8n execute` printed for each run is kept there with every API key blanked, to read when a check fails.
RAW_DIR: Path | None = None


# ---------------------------------------------------------------- the scratch world

def swap_db(dsn: str, name: str) -> str:
    return dsn.rpartition("/")[0] + "/" + name


def apply_remaining_migrations(dsn: str) -> None:
    """build_sample_report.seed applies 001, 002, 006, 007 and 008; the API needs 003 and the report runs need 009 too."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in ("003_api_keys.sql", "009_report_runs.sql"):
            conn.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


def add_tenants(dsn: str, sample_tenant: str) -> dict[str, dict]:
    """The tenants the workflow will be asked about, each with an API key. Returns name -> {tenant, key}."""
    second, no_store, wrong = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    keys = {"Example Outfitters": generate_key(), "Second Store": generate_key(), "No Store Co": generate_key()}
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (second,))
        cur.execute("insert into tenant (id, slug, name) values (%s, 'second', 'Second Store')", (second,))
        cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                    "values (%s, 'shopify', 'second-invented.myshopify.com', 'Second Store', %s) returning id",
                    (second, b"no credential, invented data"))
        connection = cur.fetchone()[0]
        payload = [{"tenant_id": second, "connection_id": str(connection), "metric_key": p.metric_key, "observed_on": p.observed_on.isoformat(),
                    "value_numeric": p.value_numeric, "currency": p.currency, "raw": {}} for p in demo_points(seed=7)]
        cur.execute("select upsert_snapshots(%s::jsonb)", (json.dumps(payload),))
        for run in DEMO_RUNS:
            if run.status != "failed":
                cur.execute("insert into sync_run (tenant_id, connection_id, trigger, window_start, window_end, status, finished_at) "
                            "values (%s, %s, 'schedule', %s, %s, %s, now())", (second, connection, run.window_start, run.window_end, run.status))
        cur.execute("select set_config('app.tenant_id', %s, false)", (no_store,))
        cur.execute("insert into tenant (id, slug, name) values (%s, 'nostore', 'No Store Co')", (no_store,))
        cur.execute("insert into platform_connection (tenant_id, platform, external_account, display_name, credentials_enc) "
                    "values (%s, 'meta', 'act_5', 'ads only', %s)", (no_store, b"no credential, invented data"))
        cur.execute("select set_config('app.tenant_id', '', false)")
        for name, tenant in (("Example Outfitters", sample_tenant), ("Second Store", second), ("No Store Co", no_store)):
            cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, %s, 'operator')",
                        (tenant, hash_key(keys[name]), name))
    # the fourth tenant's key is well formed and unknown to the hub, so its call is refused with 401
    keys["Wrong Key Co"] = generate_key()
    return {"Example Outfitters": {"tenant": sample_tenant, "key": keys["Example Outfitters"]},
            "Second Store": {"tenant": second, "key": keys["Second Store"]},
            "No Store Co": {"tenant": no_store, "key": keys["No Store Co"]},
            "Wrong Key Co": {"tenant": wrong, "key": keys["Wrong Key Co"]}}


# ---------------------------------------------------------------- the servers

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SlackStandIn:
    """Records every JSON body posted to it and answers 200 `ok`, as an incoming webhook does."""

    def __init__(self):
        self.posts: list[dict] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                try:
                    stand_in.posts.append(json.loads(body))
                except ValueError:
                    stand_in.posts.append({"unparsed": True})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_api(dsn: str, log_path: Path) -> tuple[subprocess.Popen, int, object]:
    port = free_port()
    env = dict(os.environ, DATABASE_URL=dsn, DASHBOARD_ORIGIN="http://127.0.0.1:3100")
    log = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit("the API stopped at startup, see %s" % log_path)
        try:
            if httpx.get("http://127.0.0.1:%d/ready" % port, timeout=1).status_code == 200:
                return proc, port, log
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    proc.terminate()
    raise SystemExit("the API did not become ready in 40 seconds")


# ---------------------------------------------------------------- n8n

def run_n8n(image: str, staged: Path, env: dict[str, str], label: str) -> dict:
    """One throwaway container: import the workflow and execute it, and return the execution JSON `n8n execute` prints."""
    env_file = staged / "env.list"
    env_file.write_text("".join("%s=%s\n" % (k, v) for k, v in env.items()), encoding="utf-8")
    name = "n8n-verify-%s" % uuid.uuid4().hex[:8]
    cmd = ["docker", "run", "--rm", "--name", name, "--env-file", str(env_file), "-v", "%s:/data" % staged, "--entrypoint", "sh", image,
           "-c", "n8n import:workflow --input=/data/workflow.json >/dev/null && n8n execute --id=%s" % WORKFLOW_ID]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        raise SystemExit("run %s: the container did not finish in %d seconds" % (label, DOCKER_TIMEOUT))
    finally:
        env_file.unlink(missing_ok=True)
    text = done.stdout
    if RAW_DIR is not None:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / ("run-%s.txt" % label)).write_text(re.sub(r"aah_[A-Za-z0-9_-]+", "<api key>", text + "\n" + done.stderr), encoding="utf-8")
    start = text.find("\n{")
    if start < 0:
        raise SystemExit("run %s: n8n printed no execution (exit %s):\n%s\n%s" % (label, done.returncode, text[-1500:], done.stderr[-1500:]))
    execution, _ = json.JSONDecoder().raw_decode(text[start + 1:])
    execution["_exit_code"] = done.returncode
    return execution


def schedule_fires(image: str, staged: Path) -> list[str]:
    """The next fire times of the workflow's Schedule Trigger, computed by the `cron` library inside the n8n image (what n8n schedules with)."""
    for name in ("cron_probe.js", "cron_probe.sh"):
        shutil.copyfile(Path(__file__).resolve().parent / name, staged / name)
    done = subprocess.run(["docker", "run", "--rm", "-v", "%s:/data" % staged, "--entrypoint", "sh", image, "/data/cron_probe.sh"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("next six fires:")), "")
    return [part.strip() for part in line.partition(":")[2].split("|") if part.strip()]


def outputs(execution: dict, node: str) -> list[list[dict]]:
    """For each run of a node, per output index, the list of item json: [[output0 items, output1 items], ...]."""
    runs = execution["data"]["resultData"]["runData"].get(node, [])
    return [[[item["json"] for item in (branch or [])] for branch in run.get("data", {}).get("main", [])] for run in runs]


def items(execution: dict, node: str, output: int = 0) -> list[dict]:
    return [item for run in outputs(execution, node) for item in (run[output] if len(run) > output else [])]


def ran(execution: dict, node: str) -> bool:
    return node in execution["data"]["resultData"]["runData"]


def node_error(execution: dict) -> str | None:
    err = execution["data"]["resultData"].get("error")
    return (err or {}).get("message")


def describe(execution: dict) -> list[str]:
    lines = ["status %s, finished %s, mode %s, trigger %s" % (execution.get("status"), execution.get("finished"), execution.get("mode"),
                                                              execution["data"]["executionData"]["runtimeData"]["triggerNode"]["name"])]
    run_data = execution["data"]["resultData"]["runData"]
    for node in sorted(run_data, key=lambda n: run_data[n][0]["executionIndex"]):
        per_run = outputs(execution, node)
        counts = ["+".join(str(len(branch)) for branch in run) for run in per_run]
        lines.append("  %-34s outputs (items) %s" % (node, ", ".join(counts)))
    for node in sorted(n for n in run_data if n.startswith("Result: ")):
        for item in items(execution, node):
            lines.append("    %s | %s | %s | %s" % (node.replace("Result: ", ""), item.get("tenant"), item.get("status"), item.get("detail")))
    if node_error(execution):
        lines.append("  execution error: %s" % node_error(execution))
    return lines


# ---------------------------------------------------------------- the checks

class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, condition: bool, what: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(what)
            print("FAILED CHECK: %s" % what)


def sql(dsn: str, query: str, params=()):
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def results(execution: dict) -> dict[str, tuple[str, str]]:
    out = {}
    for node in execution["data"]["resultData"]["runData"]:
        if node.startswith("Result: "):
            for item in items(execution, node):
                out[item["tenant"]] = (item["status"], item["detail"])
    return out


def verify(image: str, dsn: str, summary: list[str]) -> int:
    checks = Checks()
    scratch = "hub_n8n_" + uuid.uuid4().hex[:8]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute('create database "%s"' % scratch)
    scratch_dsn = swap_db(dsn, scratch)
    api = log = None
    staged = Path(tempfile.mkdtemp(prefix="n8nverify_"))
    try:
        version = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "n8n --version"], capture_output=True, text=True,
                                 timeout=120).stdout.strip()
        summary.append("n8n %s, image %s, workflow %s, month %s" % (version, image, WORKFLOW_ID, MONTH))
        (staged / "workflow.json").write_text(render(), encoding="utf-8", newline="\n")
        fires = schedule_fires(image, staged)
        summary.append("Schedule Trigger, next fires by n8n's own cron library: " + ", ".join(fires))
        checks.ok(len(fires) == 6 and all(f.endswith("-01T13:00:00.000Z") for f in fires) and fires[0] == "2026-11-01T13:00:00.000Z",
                  "the schedule fires at 13:00 UTC on the first of every month, by n8n's cron library")
        tenant, _store, website, ads, search = sample.seed(scratch_dsn)
        asyncio.run(sample.sync_website(scratch_dsn, tenant, website))
        asyncio.run(sample.sync_ads_and_search(scratch_dsn, tenant, ads, search))
        apply_remaining_migrations(scratch_dsn)
        world = add_tenants(scratch_dsn, tenant)
        api, port, log = start_api(scratch_dsn, staged / "api.log")
        hub = "http://host.docker.internal:%d" % port
        with SlackStandIn() as slack:
            hook = "http://host.docker.internal:%d/hook" % slack.port
            everyone = [{"name": n, "api_key": v["key"]} for n, v in world.items()]
            workers = [t for t in everyone if t["name"] in ("Example Outfitters", "Second Store")]

            def base(tenants, **extra):
                return {"N8N_BLOCK_ENV_ACCESS_IN_NODE": "false", "HUB_BASE_URL": hub, "HUB_TENANTS_JSON": json.dumps(tenants),
                        "HUB_REPORT_MONTH": MONTH, "HUB_AS_OF": AS_OF, "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                        **extra}

            labels = {v["tenant"]: n for n, v in world.items()}

            def report_rows():
                rows = sql(scratch_dsn, "select r.tenant_id::text, r.status, r.month, r.deck_sha256, r.delivered_at is not null from report_run r")
                return sorted((labels[r[0]], *r[1:]) for r in rows)

            # ---- run 1: delivery off, two tenants work, one has no store (422), one has a key the hub does not know (401)
            e1 = run_n8n(image, staged, base(everyone), "1")
            summary += ["", "RUN 1: delivery off, four tenants (two work, one cannot be built, one has a wrong key)"] + describe(e1)
            build1 = items(e1, "Build the report", 0)
            checks.ok(e1["status"] == "error" and e1["_exit_code"] != 0, "run 1 ends as an error, the visible alert for the two failed tenants")
            checks.ok(len(build1) == 2 and {b["status"] for b in build1} == {"created"}, "run 1: the report endpoint answered 'created' for the two tenants that work")
            checks.ok({b["slides"] for b in build1} == {10, 7},
                      "run 1: the sample tenant's deck has ten slides (store, website, ads and search) and the store only tenant's seven")
            checks.ok(len(items(e1, "Build the report", 1)) == 2, "run 1: the two failed tenants went down the error output of the report call")
            r1 = results(e1)
            checks.ok(r1.get("Example Outfitters", ("",))[0] == "built, delivery off" and r1.get("Second Store", ("",))[0] == "built, delivery off",
                      "run 1: the tenants that work are 'built, delivery off', not stopped by the ones that failed")
            checks.ok(r1.get("No Store Co", ("",))[0] == "failed" and "no active connection" in r1["No Store Co"][1],
                      "run 1: the tenant with no store failed with the hub's reason")
            checks.ok(r1.get("Wrong Key Co", ("",))[0] == "failed", "run 1: the tenant with a wrong key failed")
            checks.ok(ran(e1, "Stop and Error") and "No Store Co" in (node_error(e1) or "") and "Wrong Key Co" in (node_error(e1) or ""),
                      "run 1: Stop and Error ran and names both failed tenants")
            checks.ok(not ran(e1, "Post the summary to Slack") and not ran(e1, "Email the deck") and slack.posts == [],
                      "run 1: nothing was sent while delivery is off")
            db1 = report_rows()
            checks.ok([(r[0], r[1]) for r in db1] == [("Example Outfitters", "done"), ("No Store Co", "failed"), ("Second Store", "done")]
                      and not any(r[4] for r in db1), "run 1: the database holds two done runs and a failed one and nothing delivered")
            sha1 = {r[0]: r[3] for r in db1 if r[1] == "done"}

            # ---- run 2: the same again
            e2 = run_n8n(image, staged, base(everyone), "2")
            summary += ["", "RUN 2: the same month again, delivery off"] + describe(e2)
            build2 = items(e2, "Build the report", 0)
            checks.ok(len(build2) == 2 and {b["status"] for b in build2} == {"replayed"}, "run 2: the endpoint answered 'replayed' for both tenants, building nothing")
            checks.ok({b["sha256"] for b in build2} == set(sha1.values()), "run 2: the decks are the same bytes as run 1 (same hashes)")
            checks.ok(report_rows() == db1, "run 2: the database is exactly as run 1 left it (no new row, no changed deck)")
            checks.ok(slack.posts == [], "run 2: nothing was sent")

            # ---- run 3: Slack on
            e3 = run_n8n(image, staged, base(everyone, DELIVER_SLACK="true", SLACK_WEBHOOK_URL=hook), "3")
            summary += ["", "RUN 3: Slack on"] + describe(e3)
            r3 = results(e3)
            summaries = [p["text"] for p in slack.posts if p["text"].startswith("Monthly report for")]
            alerts = [p["text"] for p in slack.posts if p["text"].startswith("Monthly report FAILED")]
            checks.ok(len(summaries) == 2 and any("Example Outfitters" in s for s in summaries) and any("Second Store" in s for s in summaries),
                      "run 3: the Slack stand-in received one summary for each tenant that works")
            checks.ok(len(alerts) == 1 and "No Store Co" in alerts[0] and "Wrong Key Co" in alerts[0], "run 3: and one alert naming both failed tenants")
            checks.ok(r3.get("Example Outfitters", ("",))[0] == "delivered" and r3.get("Second Store", ("",))[0] == "delivered", "run 3: both results say delivered")
            db3 = report_rows()
            checks.ok([r[4] for r in db3 if r[1] == "done"] == [True, True], "run 3: the hub recorded both decks as delivered")
            checks.ok({r[0]: r[3] for r in db3 if r[1] == "done"} == sha1, "run 3: delivering changed no deck")
            posts_after_3 = len(slack.posts)

            # ---- run 4: Slack on again
            e4 = run_n8n(image, staged, base(everyone, DELIVER_SLACK="true", SLACK_WEBHOOK_URL=hook), "4")
            summary += ["", "RUN 4: Slack on, the same month again"] + describe(e4)
            r4 = results(e4)
            checks.ok(all(b["delivered"] is True and b["status"] == "replayed" for b in items(e4, "Build the report", 0)),
                      "run 4: the endpoint says both decks were already delivered")
            checks.ok(r4.get("Example Outfitters", ("",))[0] == "skipped, already delivered" and r4.get("Second Store", ("",))[0] == "skipped, already delivered",
                      "run 4: both results say skipped, already delivered")
            checks.ok(not ran(e4, "Post the summary to Slack"), "run 4: the Slack post node did not run for any tenant")
            new_posts = slack.posts[posts_after_3:]
            checks.ok(len(new_posts) == 1 and new_posts[0]["text"].startswith("Monthly report FAILED"),
                      "run 4: the only new message is the alert for the tenants that still fail, no summary was sent twice")
            checks.ok(len([p for p in slack.posts if p["text"].startswith("Monthly report for")]) == 2, "run 4: two summaries in all, across both Slack runs")

            # ---- run 5: only the tenants that work
            e5 = run_n8n(image, staged, base(workers, DELIVER_SLACK="true", SLACK_WEBHOOK_URL=hook), "5")
            summary += ["", "RUN 5: only the tenants that work"] + describe(e5)
            checks.ok(e5["status"] == "success" and e5["finished"] is True and e5["_exit_code"] == 0, "run 5: the execution succeeds and finishes")
            checks.ok(not ran(e5, "Stop and Error") and ran(e5, "All done"), "run 5: no alert, the workflow ends at 'All done'")
            checks.ok(len(slack.posts) == posts_after_3 + 1, "run 5: nothing more was sent")

            # ---- run 6: email on, no SMTP credential, a month nobody has built yet
            e6 = run_n8n(image, staged, base(workers, HUB_REPORT_MONTH="2026-09", DELIVER_EMAIL="true", EMAIL_FROM="reports@example.test",
                                              EMAIL_TO="client@example.test"), "6")
            summary += ["", "RUN 6: email on with no SMTP credential, another month"] + describe(e6)
            r6 = results(e6)
            checks.ok(len(items(e6, "Fetch the deck", 0)) == 2, "run 6: the deck was fetched for both tenants (a binary answer from the hub)")
            checks.ok(len(items(e6, "Email sent?", 1)) == 2 and not items(e6, "Email sent?", 0),
                      "run 6: the Send Email node had no credential, and neither tenant's item was read as sent")
            checks.ok(all(r6.get(n, ("",))[0] == "failed" and "email was not sent" in r6[n][1] for n in ("Example Outfitters", "Second Store")),
                      "run 6: the email step is a failed result for both tenants and says so")
            checks.ok(ran(e6, "Stop and Error") and not ran(e6, "Mark delivered"), "run 6: the execution is an error and nothing was marked delivered")
            sep = [r for r in report_rows() if r[2] == "2026-09"]
            checks.ok(len(sep) == 2 and not any(r[4] for r in sep), "run 6: the September decks exist and are not delivered")

        summary += ["", "%d checks passed, %d failed" % (checks.passed, len(checks.failures))] + ["FAILED: " + f for f in checks.failures]
        return 0 if not checks.failures else 1
    finally:
        if api is not None:
            api.terminate()
            try:
                api.wait(timeout=15)
            except subprocess.TimeoutExpired:
                api.kill()
        if log is not None:
            log.close()
        for attempt in range(6):            # Windows can hold the API's log for a moment after the process has gone
            shutil.rmtree(staged, ignore_errors=True)
            if not staged.exists():
                break
            time.sleep(0.5)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute('drop database if exists "%s" with (force)' % scratch)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", default="n8nio/n8n:2.44.6")
    p.add_argument("--summary", help="write the summary of what each run did to this file")
    p.add_argument("--raw-dir", help="keep what n8n printed for each run here, with the API keys blanked")
    args = p.parse_args(argv)
    global RAW_DIR
    RAW_DIR = Path(args.raw_dir) if args.raw_dir else None
    summary: list[str] = []
    code = 1
    try:
        code = verify(args.image, os.environ.get("ANALYTICS_TEST_DSN", DEFAULT_DSN), summary)
    finally:
        text = "\n".join(summary)
        if "aah_" in text:
            text = "the summary was withheld: it held something that looks like an API key"
        print(text)
        if args.summary:
            Path(args.summary).write_text(text + "\n", encoding="utf-8", newline="\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
