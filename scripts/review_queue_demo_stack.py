# LIFESPAN: permanent
"""Stand up the hub's review queue with something in it, and leave it running for an internal dashboard (Appsmith, or anything that calls HTTP) to read.

    python scripts/review_queue_demo_stack.py --port 8099 --state state.json

What it does: makes a scratch database (`analytics_review_demo`) on the Postgres of ANALYTICS_TEST_DSN, applies the migrations, makes one tenant and an operator
API key, stores a made-up applicant's facts, starts the hub API on 0.0.0.0:<port>, starts the stand-in application pages, and runs one application against a form
with a question nobody can pre-answer, so the queue holds one pending review task. It then writes the address, the key and the run id to the state file and waits
until it is stopped (Ctrl+C or a signal), when it stops the API and the stand-in pages. The Postgres, Redis-less configuration needs only Postgres and a local Chromium.

The key is random and dies with the scratch database, which this script drops on the way out.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import psycopg  # noqa: E402

from app.api.auth import generate_key, hash_key  # noqa: E402
from app.jobs.fake_ats import FakeATS  # noqa: E402

BASE_DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
FACTS = {"first_name": "Sam", "last_name": "Rivera", "full_name": "Sam Rivera", "email": "sam.rivera@example.test", "phone": "+15555550123",
         "linkedin": "https://linkedin.com/in/sam-rivera-example", "work_authorization": "Yes", "referral_source": "LinkedIn"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--state", required=True)
    args = ap.parse_args()
    name = "analytics_review_demo"
    admin_dsn = BASE_DSN.rsplit("/", 1)[0] + "/postgres"
    dsn = BASE_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute('drop database if exists "%s" with (force)' % name)
        c.execute('create database "%s"' % name)
    key = generate_key()
    tenant = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for f in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "019_job_pipeline.sql"):
            cur.execute((ROOT / "migrations" / f).read_text(encoding="utf-8"))
        cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
        cur.execute("insert into tenant (id, slug, name) values (%s, 'demo', 'Demo')", (tenant,))
        cur.execute("insert into api_key (tenant_id, key_hash, label, role) values (%s, %s, 'demo', 'operator')", (tenant, hash_key(key)))
        for k, v in FACTS.items():
            cur.execute("insert into applicant_fact (tenant_id, fact_key, fact_value) values (%s, %s, %s)", (tenant, k, v))
    tmp = Path(tempfile.mkdtemp(prefix="reviewdemo-"))
    resume = tmp / "Sam_Rivera_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% stand-in resume\n")
    env = dict(os.environ, DATABASE_URL=dsn, DASHBOARD_ORIGIN="http://127.0.0.1:3100", JOBS_S3_BUCKET="unused", JOBS_S3_ENDPOINT="http://127.0.0.1:8333",
               JOBS_S3_ACCESS_KEY="webtestaccesskey", JOBS_S3_SECRET_KEY="webtest-secret-key-not-real", JOBS_S3_REGION="us-east-1", JOBS_RESUME_FILE=str(resume), JOBS_SUBMIT="1")
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", str(args.port)], cwd=ROOT, env=env,
                           stdout=open(tmp / "api.log", "wb"), stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                if httpx.get("http://127.0.0.1:%d/ready" % args.port, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        with FakeATS() as ats:
            r = httpx.post("http://127.0.0.1:%d/api/jobs/apply" % args.port, headers={"Authorization": "Bearer " + key}, json={"apply_url": ats.url + "/gh/jobs/2"}, timeout=20)
            run_id = r.json()["run_id"]
            for _ in range(200):
                tasks = httpx.get("http://127.0.0.1:%d/api/jobs/review" % args.port, headers={"Authorization": "Bearer " + key}, timeout=10).json()["tasks"]
                if tasks:
                    break
                time.sleep(0.25)
            Path(args.state).write_text(json.dumps({"hub": "http://127.0.0.1:%d" % args.port, "key": key, "run_id": run_id, "tasks": len(tasks), "ats": ats.url}), encoding="utf-8")
            print("ready: %d pending task(s)" % len(tasks), flush=True)
            stop = []
            signal.signal(signal.SIGINT, lambda *a: stop.append(1))
            signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
            while not stop:
                time.sleep(0.5)
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
        with psycopg.connect(admin_dsn, autocommit=True) as c:
            c.execute('drop database if exists "%s" with (force)' % name)
    return 0



if __name__ == "__main__":
    sys.exit(main())
