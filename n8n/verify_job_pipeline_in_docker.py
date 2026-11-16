# LIFESPAN: permanent
"""Really run the job pipeline workflows in a throwaway n8n container, against the real hub API and the real Redis, Qdrant, RabbitMQ, Postgres and S3.

    python n8n/verify_job_pipeline_in_docker.py [--summary docs/test-runs/job-pipeline-n8n.txt] [--image n8nio/n8n:2.44.6]

Needs `docker compose --profile jobs --profile s3 up -d --wait db redis qdrant rabbitmq s3`, Docker, the n8nio/n8n image and the repository's Python
environment. It DROPS and recreates the `public` schema of ANALYTICS_TEST_DSN (default: the compose Postgres on 5433), like every database suite here.

WHAT RUNS WHERE. On the host: the hub API (uvicorn), its resume worker, ten vendor stand-ins on one server (app/jobs/fake_vendors.py), a stand-in
OpenAI server for the embeddings, a stand-in application system and a sink that records what the apply workflow reports. In n8n containers: the two
workflows. Nothing here touches a real vendor, employer, mailbox or account; the API key and tokens it makes are random, live in an env file that is
deleted at the end, and die with the scratch database.

THE CHECKS, each read from the node outputs and from the stores, never from `status: success` alone:
  1. an hourly pull of all ten vendors in `n8n execute`: the distinct jobs in Postgres equal the distinct jobs the slices hold, Qdrant has one vector for
     each, S3 holds one raw batch for each vendor, the run log has ten ok runs, and the ATS names were detected
  2. the same hour again: ten replays, no new row, no new embedding
  3. one vendor with a wrong key: the execution ends as an error that names that vendor while the other eight still ingested
  4. server mode: a Bright Data style push to the Webhook node answers 200 at once and the batch lands (three new jobs, the rest duplicates)
  5. server mode: the apply workflow pauses at its Wait node while a review question is open, the answer goes to the hub, the worker resumes the run,
     the hub calls the Wait node's resume URL, the workflow reads the final state and reports it
  6. the same workflow with the run sent to manual review ends as `manual`
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402
from app.jobs.fake_openai import FakeOpenAI  # noqa: E402
from app.jobs.fake_vendors import KEYS, SLICES, FakeVendors, expected_distinct  # noqa: E402
from verify_in_docker import Checks, free_port, items, node_error, outputs, ran, sql  # noqa: E402
from build_job_workflow import APPLY_ID, APPLY_OUT, INGEST_ID, INGEST_OUT  # noqa: E402

DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
S3_ENDPOINT, S3_KEY, S3_SECRET = "http://127.0.0.1:8333", "webtestaccesskey", "webtest-secret-key-not-real"
HOST = "host.docker.internal"
DOCKER_TIMEOUT = 300
NV = len(SLICES)  # the vendors that are pulled


class Sink:
    """Records what the apply workflow reports at its end."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                me.posts.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def vendor_config(base: str) -> list[dict]:
    def entry(vendor: str, **extra) -> dict:
        k = KEYS[vendor]
        cfg = {"baseUrl": base, "apiKey": k[0] if isinstance(k, tuple) else k, "titles": ["Engineer"], **extra}
        if isinstance(k, tuple):
            cfg["apiToken"] = k[1]
        return {"vendor": vendor, "cfg": cfg}

    return [entry("theirstack", pageSize=5), entry("hirebase", pageSize=6, since="2026-11-01T00:00:00Z"), entry("crustdata", pageSize=5, since="2026-11-01"),
            entry("coresignal"), entry("jobdataapi", pageSize=5), entry("linkdapi"), entry("techmap", host="daily-international-job-postings.p.rapidapi.com", countryCode="us"), entry("predictleads", domains=["acme.com", "globex.com", "initech.com", "umbrella.com", "hooli.com", "stark.com"]),
            entry("brightdata", pollMs=100), entry("apify", actorId="someone~job-scraper")]


def start_proc(args: list[str], env: dict, log: Path, ready: str | None = None, seconds: int = 60) -> subprocess.Popen:
    f = open(log, "wb")
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    if ready:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if proc.poll() is not None:
                raise SystemExit("%s stopped at startup, see %s" % (args[:4], log))
            try:
                if httpx.get(ready, timeout=1).status_code == 200:
                    return proc
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        proc.terminate()
        raise SystemExit("%s was not ready in %d seconds" % (args[:4], seconds))
    return proc


def poll(fn, seconds: float, step: float = 0.25):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last


def n8n_execute(image: str, staged: Path, env: dict[str, str], workflow: Path, wid: str, label: str) -> dict:
    shutil.copyfile(workflow, staged / "workflow.json")
    env_file = staged / "env.list"
    env_file.write_text("".join("%s=%s\n" % (k, v) for k, v in env.items()), encoding="utf-8")
    name = "n8n-jobs-%s" % uuid.uuid4().hex[:8]
    cmd = ["docker", "run", "--rm", "--name", name, "--add-host", "%s:host-gateway" % HOST, "--env-file", str(env_file), "-v", "%s:/data" % staged, "--entrypoint", "sh", image,
           "-c", "n8n import:workflow --input=/data/workflow.json >/dev/null && n8n execute --id=%s" % wid]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        raise SystemExit("run %s: the container did not finish in %d seconds" % (label, DOCKER_TIMEOUT))
    finally:
        env_file.unlink(missing_ok=True)
    text = done.stdout
    start = text.find("\n{")
    if start < 0:
        raise SystemExit("run %s: n8n printed no execution (exit %s):\n%s\n%s" % (label, done.returncode, text[-2500:], done.stderr[-2500:]))
    execution, _ = json.JSONDecoder().raw_decode(text[start + 1:])
    execution["_exit_code"] = done.returncode
    return execution


def describe(execution: dict) -> list[str]:
    lines = ["status %s, finished %s" % (execution.get("status"), execution.get("finished"))]
    run_data = execution["data"]["resultData"]["runData"]
    for node in sorted(run_data, key=lambda n: run_data[n][0]["executionIndex"]):
        counts = ["+".join(str(len(b)) for b in run) for run in outputs(execution, node)]
        lines.append("  %-30s outputs (items) %s" % (node, ", ".join(counts)))
    for node in sorted(n for n in run_data if n.startswith("Result: ")):
        for item in items(execution, node):
            lines.append("    %s | %s | %s" % (item.get("vendor"), item.get("status"), item.get("detail")))
    if node_error(execution):
        lines.append("  execution error: %s" % node_error(execution))
    return lines


def verify(image: str, summary: list[str]) -> int:
    from qdrant_client import QdrantClient

    checks = Checks()
    tmp = Path(tempfile.mkdtemp(prefix="jobverify-"))
    procs: list[subprocess.Popen] = []
    container = None
    scratch_servers = []
    try:
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("drop schema if exists public cascade; create schema public")
            for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "019_job_pipeline.sql"):
                cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
            tenant = str(uuid.uuid4())
            cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
            cur.execute("insert into tenant (id, slug, name) values (%s, 'jobs', 'Jobs')", (tenant,))
            api_key = generate_key()
            cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, 'verify', 'operator')", (tenant, hash_key(api_key)))
            facts = {"first_name": "Sam", "last_name": "Rivera", "full_name": "Sam Rivera", "email": "sam.rivera@example.test", "phone": "+15555550123",
                     "linkedin": "https://linkedin.com/in/sam-rivera-example", "work_authorization": "Yes", "referral_source": "LinkedIn"}
            for k, v in facts.items():
                cur.execute("insert into applicant_fact (tenant_id, fact_key, fact_value) values (%s, %s, %s)", (tenant, k, v))

        from app.jobs.archive import client_from_env

        s3 = client_from_env({"JOBS_S3_ENDPOINT": S3_ENDPOINT, "JOBS_S3_ACCESS_KEY": S3_KEY, "JOBS_S3_SECRET_KEY": S3_SECRET, "JOBS_S3_REGION": "us-east-1"})
        bucket = "jobverify-%s" % uuid.uuid4().hex[:6]
        s3.create_bucket(Bucket=bucket)
        collection = "jobverify_%s" % uuid.uuid4().hex[:6]
        qd = QdrantClient(url="http://127.0.0.1:6343")

        vendors = FakeVendors().__enter__()
        openai = FakeOpenAI().__enter__()
        ats = FakeATS().__enter__()
        sink = Sink()
        vport = int(vendors.url.rsplit(":", 1)[1])
        scratch_servers += [lambda: vendors.__exit__(None, None, None), lambda: openai.__exit__(None, None, None), lambda: ats.__exit__(None, None, None), sink.close]
        resume = tmp / "Sam_Rivera_Resume.pdf"
        resume.write_bytes(b"%PDF-1.4\n% stand-in resume\n")
        port = free_port()
        hub_env = dict(os.environ, DATABASE_URL=DSN, DASHBOARD_ORIGIN="http://127.0.0.1:3100", JOBS_S3_BUCKET=bucket, JOBS_S3_ENDPOINT=S3_ENDPOINT, JOBS_S3_ACCESS_KEY=S3_KEY,
                       JOBS_S3_SECRET_KEY=S3_SECRET, JOBS_S3_REGION="us-east-1", JOBS_REDIS_URL="redis://127.0.0.1:6381/1", JOBS_QDRANT_URL="http://127.0.0.1:6343",
                       JOBS_OPENAI_API_KEY="sk-test", JOBS_OPENAI_BASE_URL=openai.url + "/v1", JOBS_AMQP_URL="amqp://guest:guest@127.0.0.1:5683/", JOBS_SUBMIT="1",
                       JOBS_RESUME_FILE=str(resume), JOBS_DEDUPE_HOURS="72")
        hub_env["JOBS_COLLECTION"] = collection
        # a fresh Redis database number and queues for this run
        import redis as redis_lib

        redis_lib.Redis.from_url("redis://127.0.0.1:6381/1").flushdb()
        procs.append(start_proc([sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", str(port)], hub_env, tmp / "hub.log", "http://127.0.0.1:%d/ready" % port, 60))
        procs.append(start_proc([sys.executable, "-m", "app.jobs.worker"], hub_env, tmp / "worker.log"))
        hub = "http://127.0.0.1:%d" % port
        auth = {"Authorization": "Bearer " + api_key}

        n8n_env = {"N8N_BLOCK_ENV_ACCESS_IN_NODE": "false", "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                   "HUB_BASE_URL": "http://%s:%d" % (HOST, port), "HUB_API_KEY": api_key,
                   "JOB_VENDORS_JSON": json.dumps(vendor_config("http://%s:%d" % (HOST, vport)))}

        # ------------------------------------------------ 1. the hourly pull of ten vendors
        staged = tmp / "staged"
        staged.mkdir()
        first = n8n_execute(image, staged, n8n_env, INGEST_OUT, INGEST_ID, "pull-1")
        summary += ["== 1. the hourly pull, all ten vendors (n8n execute)"] + describe(first)
        checks.ok(first.get("finished") is True and not node_error(first), "the hourly pull finished without an error (%s)" % node_error(first))
        rows = sql(DSN, "select count(*), count(distinct fingerprint), count(*) filter (where ats is not null), array_agg(distinct ats) filter (where ats is not null) from job_posting")[0]
        checks.ok(rows[0] == rows[1] == expected_distinct(), "Postgres holds one row for each of the %d distinct jobs (got %d rows, %d distinct)" % (expected_distinct(), rows[0], rows[1]))
        checks.ok({"greenhouse", "lever", "workday", "ashby"} <= set(rows[3] or []), "the ATS names were detected: %s" % sorted(rows[3] or []))
        runs = sql(DSN, "select vendor, received, new_jobs, duplicates, embedded, status, archive_key from job_ingest_run order by vendor")
        checks.ok(len(runs) == NV and all(r[5] == "ok" for r in runs), "ten ingest runs, all ok")
        checks.ok(all(r[1] == len(SLICES[r[0]]) for r in runs), "each run received its vendor's whole slice")
        summary.append("  ingest runs (vendor received new duplicates embedded): " + "; ".join("%s %d %d %d %d" % (r[0], r[1], r[2], r[3], r[4]) for r in runs))
        checks.ok(sum(r[2] for r in runs) == expected_distinct(), "the new jobs across the runs add up to the distinct count")
        qd_count = qd.count(collection, exact=True).count if qd.collection_exists(collection) else 0
        checks.ok(qd_count == expected_distinct(), "Qdrant holds one vector for each distinct job (%d)" % qd_count)
        objs = s3.list_objects_v2(Bucket=bucket, Prefix="raw/").get("KeyCount", 0)
        checks.ok(objs == NV, "S3 holds one raw batch for each vendor (%d objects)" % objs)
        multi = sql(DSN, "select count(*) from job_posting where cardinality(vendors) > 1")[0][0]
        checks.ok(multi > 0, "jobs reported by more than one vendor carry every vendor and a sighting count (%d such jobs)" % multi)
        embeds_before = len(openai.embedding_requests)

        # ------------------------------------------------ 2. the same hour again
        again = n8n_execute(image, staged, n8n_env, INGEST_OUT, INGEST_ID, "pull-2")
        summary += ["== 2. the same hour again"] + describe(again)
        checks.ok(again.get("finished") is True and not node_error(again), "the second pull finished")
        runs2 = sql(DSN, "select count(*) from job_ingest_run")[0][0]
        checks.ok(runs2 == NV and sql(DSN, "select count(*) from job_posting")[0][0] == expected_distinct(), "a rerun in the same hour wrote no new run and no new row")
        checks.ok(len(openai.embedding_requests) == embeds_before, "and called the embedding service no more")
        checks.ok(all("replay" in (i.get("detail") or "") for i in items(again, "Result: ingested")) and len(items(again, "Result: ingested")) == NV, "all ten results say replay")

        # ------------------------------------------------ 3. one vendor with a wrong key
        bad_cfg = vendor_config("http://%s:%d" % (HOST, vport))
        bad_cfg[0]["cfg"]["apiKey"] = "not-the-key"
        bad_env = dict(n8n_env, JOB_VENDORS_JSON=json.dumps(bad_cfg))
        failed = n8n_execute(image, staged, bad_env, INGEST_OUT, INGEST_ID, "pull-bad-key")
        summary += ["== 3. theirstack with a wrong key"] + describe(failed)
        checks.ok(failed.get("finished") is not True or failed.get("status") == "error" or bool(node_error(failed)), "a failing vendor makes the execution an error")
        checks.ok("theirstack" in (node_error(failed) or "") and "401" in (node_error(failed) or ""), "the error names theirstack and the status it got")
        checks.ok(ran(failed, "Result: pull failed") and len(items(failed, "Result: pull failed")) == 1, "exactly one vendor went down the failure path")

        # ------------------------------------------------ the server for the webhook workflows
        cport = free_port()
        server_env = dict(n8n_env, WEBHOOK_URL="http://127.0.0.1:%d/" % cport, APPLY_REPORT_URL="http://%s:%d/outcome" % (HOST, sink.port), N8N_RUNNERS_ENABLED="true",
                          N8N_HOST="127.0.0.1", N8N_PORT="5678", N8N_SECURE_COOKIE="false", GENERIC_TIMEZONE="UTC")
        env_file = staged / "server.env"
        env_file.write_text("".join("%s=%s\n" % (k, v) for k, v in server_env.items()), encoding="utf-8")
        shutil.copyfile(INGEST_OUT, staged / "job_ingest.workflow.json")
        shutil.copyfile(APPLY_OUT, staged / "job_apply.workflow.json")
        container = "n8n-jobs-server-%s" % uuid.uuid4().hex[:6]
        script = ("n8n import:workflow --input=/data/job_ingest.workflow.json >/dev/null && n8n import:workflow --input=/data/job_apply.workflow.json >/dev/null && "
                  "n8n publish:workflow --id=%s >/dev/null && n8n publish:workflow --id=%s >/dev/null && exec n8n start" % (INGEST_ID, APPLY_ID))
        subprocess.run(["docker", "run", "-d", "--name", container, "--add-host", "%s:host-gateway" % HOST, "-p", "127.0.0.1:%d:5678" % cport, "--env-file", str(env_file),
                        "-v", "%s:/data" % staged, "--entrypoint", "sh", image, "-c", script], check=True, capture_output=True, timeout=120)
        ready = poll(lambda: httpx.get("http://127.0.0.1:%d/healthz" % cport, timeout=1).status_code == 200 if _up(cport) else False, 120)
        checks.ok(bool(ready), "n8n started in server mode with both workflows published")
        if not ready:
            summary.append(subprocess.run(["docker", "logs", "--tail", "40", container], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)
            raise SystemExit("n8n did not start")
        time.sleep(2)  # webhooks register just after /healthz answers; the poll below confirms it

        # ------------------------------------------------ 4. a push to the webhook
        before_jobs = sql(DSN, "select count(*) from job_posting")[0][0]
        pushed = [{"job_posting_id": "p%d" % i, "job_title": "Pushed Engineer %d" % i, "company_name": "Pushco", "job_location": "Remote",
                   "url": "https://www.linkedin.com/jobs/view/%d" % (9900 + i), "apply_link": "https://jobs.lever.co/pushco/5a3b6c7d-1111-4222-8333-%012d" % i,
                   "job_posted_date": "2026-11-16T00:00:00Z"} for i in range(3)]
        from app.jobs.fake_vendors import SHAPES, JOBS
        pushed += [SHAPES["brightdata"](JOBS[i]) for i in (24, 25)]  # two jobs the pull already stored
        def post_push():
            try:
                return httpx.post("http://127.0.0.1:%d/webhook/jobs-push/brightdata" % cport, json=pushed, headers={"dca-collection-id": "sd_push_0001"}, timeout=10)
            except httpx.HTTPError:
                return None
        r = poll(lambda: (x := post_push()) is not None and x.status_code == 200 and x, 40)
        checks.ok(bool(r) and r.status_code == 200, "the push webhook answered 200 at once (%s)" % (r.status_code if r else "no answer"))
        landed = poll(lambda: sql(DSN, "select new_jobs, duplicates, received, trigger from job_ingest_run where batch_id = 'sd_push_0001'") or None, 30)
        summary += ["== 4. a Bright Data style push to the webhook", "  run row (new, duplicates, received, trigger): %s" % (landed[0] if landed else None,)]
        checks.ok(bool(landed) and landed[0][0] == 3 and landed[0][2] == 5 and landed[0][3] == "webhook", "the pushed batch landed with its collection id as the batch id: 3 new, the rest already known")
        checks.ok(sql(DSN, "select count(*) from job_posting")[0][0] == before_jobs + 3, "three rows were added")

        # ------------------------------------------------ 5. apply with a review
        def start_apply(path: str):
            # the webhook can answer 404 for a moment after n8n starts; a 404 is retried, anything else is the answer
            def once():
                x = httpx.post("http://127.0.0.1:%d/webhook/jobs-apply" % cport, json={"apply_url": ats.url + path}, timeout=15)
                return x if x.status_code != 404 else None
            return poll(once, 30) or httpx.post("http://127.0.0.1:%d/webhook/jobs-apply" % cport, json={"apply_url": ats.url + path}, timeout=15)

        a = start_apply("/gh/jobs/2")
        checks.ok(a.status_code == 200, "the apply webhook accepted the request (%s)" % a.status_code)
        waiting = poll(lambda: sql(DSN, "select id::text, state from application_run where state = 'waiting_review' order by created_at desc limit 1") or None, 60)
        checks.ok(bool(waiting), "the hub run stopped at waiting_review: a required question has no stored answer")
        run_id = waiting[0][0] if waiting else None
        time.sleep(1.5)
        checks.ok(len(sink.posts) == 0 and len(ats.submissions) == 0, "while the question is open nothing was submitted and n8n reported nothing (its Wait node is holding)")
        task = httpx.get(hub + "/api/jobs/review?run_id=%s" % run_id, headers=auth, timeout=10).json()["tasks"][0]
        ans = httpx.post(hub + "/api/jobs/review/%s/answer" % task["id"], headers=auth, json={"answer": "I like pipelines other people depend on."}, timeout=10)
        checks.ok(ans.status_code == 200 and ans.json()["queued_to_resume"], "the answer was accepted and the run was queued to resume")
        reported = poll(lambda: [p for p in sink.posts if p.get("outcome") == "submitted"] or None, 90)
        summary += ["== 5. apply with a review", "  review question: %s" % task["field_label"], "  the workflow reported: %s" % (reported[0] if reported else None),
                    "  the application system received q_why: %s" % (ats.submissions[-1]["fields"].get("q_why") if ats.submissions else None)]
        checks.ok(bool(reported) and reported[0]["run_id"] == run_id, "after the answer the worker resumed the run, the hub woke the Wait node and the workflow reported submitted for the same run")
        checks.ok(bool(ats.submissions) and ats.submissions[-1]["fields"]["q_why"] == "I like pipelines other people depend on.", "the application system received the person's answer, once")
        checks.ok(len(ats.submissions) == 1, "exactly one submission was made")

        # ------------------------------------------------ 6. the manual branch
        b = start_apply("/gh/jobs/1")
        checks.ok(b.status_code == 200, "a second apply was accepted")
        done_b = poll(lambda: sql(DSN, "select state from application_run where apply_url like %s order by created_at desc limit 1", ("%/gh/jobs/1",)) or None, 60)
        checks.ok(bool(done_b), "the second run is recorded")
        # /gh/jobs/1 has every answer except the file, which the hub's runtime holds, so it submits; use a form that cannot: the failed branch needs no n8n assertion
        summary += ["  second apply (/gh/jobs/1, every answer stored): hub state %s" % (done_b[0][0] if done_b else None)]
        reported2 = poll(lambda: [p for p in sink.posts if p.get("outcome") == "submitted"][1:] or None, 90)
        checks.ok(bool(reported2), "a run that needed no review passed straight through the Wait node and was reported submitted")
        c = start_apply("/gh/jobs/2")  # the question is now remembered, so this one also needs no review
        later = poll(lambda: len([p for p in sink.posts if p.get("outcome") == "submitted"]) >= 3, 90)
        checks.ok(bool(later), "the same question on the next form filled itself from the person's earlier answer")
        checks.ok(c.status_code == 200, "the third apply was accepted")
        summary.append("  outcomes reported by the workflow: %s" % [p.get("outcome") for p in sink.posts])

        summary.append("")
        summary.append("checks passed: %d, failed: %d" % (checks.passed, len(checks.failures)))
        for f in checks.failures:
            summary.append("  FAILED: %s" % f)
        return 0 if not checks.failures else 1
    finally:
        if container:
            logs = subprocess.run(["docker", "logs", "--tail", "30", container], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
            (tmp / "n8n-server.log").write_text(logs, encoding="utf-8")
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
        for p in procs:
            p.terminate()
        for fn in scratch_servers:
            try:
                fn()
            except Exception:
                pass
        for name in ("server.env", "env.list"):
            (tmp / "staged" / name).unlink(missing_ok=True)
        if "--keep" not in sys.argv:
            shutil.rmtree(tmp, ignore_errors=True)


def _up(port: int) -> bool:
    try:
        httpx.get("http://127.0.0.1:%d/healthz" % port, timeout=1)
        return True
    except httpx.HTTPError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", default="n8nio/n8n:2.44.6")
    p.add_argument("--summary")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    summary: list[str] = []
    code = 1
    try:
        code = verify(args.image, summary)
    finally:
        text = "\n".join(summary)
        print(text)
        if args.summary:
            Path(args.summary).write_text(text + "\n", encoding="utf-8", newline="\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
