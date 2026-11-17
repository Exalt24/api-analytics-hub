# LIFESPAN: permanent
"""Shared setup for the job pipeline suites: the services they need, a fresh schema, tenants, and the applicant and job used everywhere.

The services come from `docker compose --profile jobs --profile s3 up -d --wait` (Postgres 5433, Redis 6381, Qdrant 6343, RabbitMQ 5683, SeaweedFS
8333). A suite skips with the name of the missing service when one is down, and the database suites DROP and recreate the `public` schema of
ANALYTICS_TEST_DSN, like every other database suite here, so aim it at a scratch database.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("ANALYTICS_TEST_DSN", "postgresql://analytics:analytics@127.0.0.1:5433/analytics")
REDIS_URL = os.environ.get("JOBS_TEST_REDIS", "redis://127.0.0.1:6381/0")
QDRANT_URL = os.environ.get("JOBS_TEST_QDRANT", "http://127.0.0.1:6343")
AMQP_URL = os.environ.get("JOBS_TEST_AMQP", "amqp://guest:guest@127.0.0.1:5683/")
S3_ENDPOINT = os.environ.get("JOBS_TEST_S3", "http://127.0.0.1:8333")
# the stand-in S3 identity in web/tests/s3/seaweedfs-s3.json: a made-up pair that exists only in that test container
S3_KEY, S3_SECRET = "webtestaccesskey", "webtest-secret-key-not-real"


def port_open(url: str) -> bool:
    host, port = url.split("//")[-1].split("/")[0].split("@")[-1].split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


def needs(*names: str):
    urls = {"postgres": DSN, "redis": REDIS_URL, "qdrant": QDRANT_URL, "rabbitmq": AMQP_URL, "s3": S3_ENDPOINT}
    missing = [n for n in names if not port_open(urls[n])]
    if missing and os.environ.get("JOBS_REQUIRE_SERVICES") == "1":
        # CI sets this: a service that did not start must fail the run, never turn the suite into a pile of skips
        raise RuntimeError("JOBS_REQUIRE_SERVICES is set and %s is not reachable" % ", ".join(missing))
    return pytest.mark.skipif(bool(missing), reason="needs %s: docker compose --profile jobs --profile s3 up -d --wait" % ", ".join(missing))


def run(coro):
    return asyncio.run(coro)


def reset_schema(extra: tuple[str, ...] = ()) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public")
        for name in ("001_init.sql", "002_control_plane.sql", "003_api_keys.sql", "019_job_pipeline.sql", *extra):
            cur.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))


def add_tenant(slug: str) -> str:
    import psycopg

    tid = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select set_config('app.tenant_id', %s, false)", (tid,))
        cur.execute("insert into tenant (id, slug, name) values (%s, %s, %s)", (tid, slug, slug))
    return tid


def make_bucket(name: str):
    from app.jobs.archive import client_from_env

    s3 = client_from_env({"JOBS_S3_ENDPOINT": S3_ENDPOINT, "JOBS_S3_ACCESS_KEY": S3_KEY, "JOBS_S3_SECRET_KEY": S3_SECRET, "JOBS_S3_REGION": "us-east-1"})
    try:
        s3.create_bucket(Bucket=name)
    except Exception:
        pass
    # SeaweedFS places a new bucket's first object on a volume it grows on demand, and until that exists PutObject answers InternalError ("please try again"). Five archive tests that wrote
    # straight after creating their bucket failed that way on the CI runner (run 43992555245). A probe write, retried for up to 30 s, makes the bucket ready before any test writes to it; if it
    # never succeeds the test's own write reports the real error. The probe key sits outside every prefix a test lists.
    deadline = time.time() + 30
    while True:
        try:
            s3.put_object(Bucket=name, Key=".ready", Body=b"1")
            break
        except Exception:
            if time.time() > deadline:
                break
            time.sleep(0.5)
    return s3


def job(i: int, *, company: str = "Acme", ats_id: str | None = None, title: str | None = None, vendor_url: str | None = None) -> dict:
    """A canonical job, the shape n8n/jobs/normalize.js writes."""
    title = title or "Data Engineer %d" % i
    url = vendor_url or ("https://job-boards.greenhouse.io/%s/jobs/%s" % (company.lower(), ats_id or (4000000 + i)))
    ats_job = ats_id or str(4000000 + i)
    return {"vendor_job_id": str(i), "title": title, "company_name": company, "company_domain": company.lower() + ".com", "location": "Remote", "remote": True,
            "url": url, "apply_url": url, "ats": "greenhouse", "ats_org": company.lower(), "ats_job_id": ats_job, "posted_at": "2026-11-10T00:00:00.000Z",
            "description": "Build pipelines with Python, PostgreSQL and Kafka for %s." % company, "salary_min": 90000, "salary_max": 120000, "salary_currency": "USD",
            "salary_period": "year", "employment_type": "full_time", "technologies": ["python", "postgresql", "kafka"],
            "dedupe_key": "greenhouse|%s|%s" % (company.lower(), ats_job), "valid": True}


APPLICANT = {
    "first_name": "Sam", "last_name": "Rivera", "full_name": "Sam Rivera", "email": "sam.rivera@example.test", "phone": "+15555550123",
    "linkedin": "https://linkedin.com/in/sam-rivera-example", "work_authorization": "Yes", "location": "Manila, Philippines",
    "salary_expectation": "4000 USD a month", "referral_source": "LinkedIn",
}
