# LIFESPAN: permanent
"""The parts of the job pipeline that can be run against a real service today, and what each one printed.

    python scripts/job_pipeline_live_check.py --out docs/test-runs/job-pipeline-live.txt

Three sections, each independent, each skipped with its reason when its key is not at hand:

  1. Upstash Redis (a free database made through the management API, used for the dedupe window, then deleted): the same sliding window the tests run
     against a local Redis, over TLS, against Upstash's Redis. Needs UPSTASH_EMAIL and UPSTASH_MANAGEMENT_API_KEY in the environment.
  2. Embeddings: `text-embedding-3-small` through LangChain's OpenAIEmbeddings into a local Qdrant, then a search that has to rank the right job first.
     Needs JOBS_LIVE_OPENAI_KEY and optionally JOBS_LIVE_OPENAI_BASE_URL (OpenRouter's https://openrouter.ai/api/v1 serves it under `openai/text-embedding-3-small`).
  3. Tailoring: the loop of app/jobs/tailor.py with a real chat model (JOBS_LIVE_CHAT_MODEL, default openai/gpt-4o-mini) on the made-up applicant of the tests,
     for one job the facts cover and one that asks for tools the facts lack. It prints each round's score and grounding result.

Nothing here prints a key. The Upstash database is deleted in a finally block, and the script says whether the delete worked.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402

out: list[str] = []


def say(line: str = "") -> None:
    print(line)
    out.append(line)


def upstash() -> None:
    say("== 1. Upstash Redis: the sliding window dedupe")
    email, key = os.environ.get("UPSTASH_EMAIL"), os.environ.get("UPSTASH_MANAGEMENT_API_KEY")
    if not (email and key):
        say("  skipped: UPSTASH_EMAIL and UPSTASH_MANAGEMENT_API_KEY are not set")
        return
    import redis

    from app.jobs.dedupe import SlidingWindowDedupe

    auth = {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (email, key)).encode()).decode()}
    created = httpx.post("https://api.upstash.com/v2/redis/database", headers=auth, timeout=60,
                         json={"name": "jobs-dedupe-check-%s" % uuid.uuid4().hex[:6], "region": "global", "primary_region": "us-east-1", "read_regions": [], "tls": True})
    if created.status_code >= 300:
        say("  could not create a database: HTTP %d %s" % (created.status_code, created.text[:200]))
        return
    db = created.json()
    try:
        client = redis.Redis(host=db["endpoint"], port=int(db.get("port", 6379)), password=db["password"], ssl=True, socket_timeout=10, socket_connect_timeout=10)
        d = SlidingWindowDedupe(client, window_seconds=100, prefix="live")
        t0 = 1_000_000.0
        first = d.seen_many("t1", ["a", "b", "a"], now=t0)
        again = d.seen_many("t1", ["a", "c"], now=t0 + 50)
        slid = d.seen_many("t1", ["a"], now=t0 + 140)
        expired = d.seen_many("t1", ["b"], now=t0 + 160)
        other = d.seen_many("t2", ["a"], now=t0 + 50)
        say("  free database over TLS, Redis server %s" % client.info("server").get("redis_version"))
        say("  first call [a, b, a]        -> %s   (the repeat inside one call counts as seen)" % first)
        say("  50 s later [a, c]           -> %s" % again)
        say("  140 s, a seen at 50 s       -> %s   (the window slid: 90 s since its last sighting)" % slid)
        say("  160 s, b first seen at 0 s  -> %s   (160 s since its only sighting, expired)" % expired)
        say("  another tenant [a]          -> %s   (windows are per tenant)" % other)
        ok = first == [False, False, True] and again == [True, False] and slid == [True] and expired == [False] and other == [False]
        say("  every answer matched the rule: %s" % ok)
    except Exception as exc:
        say("  FAILED: %s" % str(exc)[:300])
    finally:
        r = httpx.delete("https://api.upstash.com/v2/redis/database/%s" % db["database_id"], headers=auth, timeout=60)
        say("  the database was deleted: %s" % (r.status_code < 300))


def embeddings() -> None:
    say("")
    say("== 2. Embeddings through LangChain into Qdrant")
    key = os.environ.get("JOBS_LIVE_OPENAI_KEY")
    if not key:
        say("  skipped: JOBS_LIVE_OPENAI_KEY is not set")
        return
    from qdrant_client import QdrantClient

    from app.jobs.embed import JobIndex, make_embeddings

    base = os.environ.get("JOBS_LIVE_OPENAI_BASE_URL") or None
    model = os.environ.get("JOBS_LIVE_EMBED_MODEL", "openai/text-embedding-3-small" if base and "openrouter" in base else "text-embedding-3-small")
    qd = QdrantClient(url=os.environ.get("JOBS_TEST_QDRANT", "http://127.0.0.1:6343"), check_compatibility=False)
    name = "live_%s" % uuid.uuid4().hex[:6]
    try:
        emb = make_embeddings(api_key=key, base_url=base, model=model)
        idx = JobIndex(qd, emb, collection=name)
        rows = [
            {"id": str(uuid.uuid4()), "title": "Data Engineer", "company_name": "Acme", "technologies": ["python", "postgresql", "kafka"], "description": "Build streaming pipelines into a warehouse."},
            {"id": str(uuid.uuid4()), "title": "Registered Nurse", "company_name": "Northside Clinic", "technologies": [], "description": "Patient care on the night shift in a busy clinic."},
            {"id": str(uuid.uuid4()), "title": "Frontend Engineer", "company_name": "Globex", "technologies": ["react", "typescript"], "description": "Design system and dashboard work in React."},
            {"id": str(uuid.uuid4()), "title": "Warehouse Operations Lead", "company_name": "Initech", "technologies": [], "description": "Run inbound and outbound shifts on a loading dock."},
        ]
        idx.add("live-tenant", rows)
        size = qd.get_collection(name).config.params.vectors.size
        say("  model %s through %s: %d vectors of width %d stored" % (model, base or "api.openai.com", idx.count("live-tenant"), size))
        for q, want in (("someone to build data pipelines in Python and SQL", "Data Engineer"), ("react developer for a component library", "Frontend Engineer"), ("night shift clinical work", "Registered Nurse")):
            hits = idx.search("live-tenant", q, k=2)
            say("  query %-52r -> %s (score %.3f)" % (q, hits[0][0]["title"], hits[0][1]))
            say("     right job first: %s" % (hits[0][0]["title"] == want))
        say("  another tenant sees: %d results" % len(idx.search("someone-else", "data pipelines", k=5)))
    except Exception as exc:
        say("  FAILED: %s" % str(exc)[:300])
    finally:
        try:
            qd.delete_collection(name)
        except Exception:
            pass


def tailoring() -> None:
    say("")
    say("== 3. Tailoring with a real chat model")
    key = os.environ.get("JOBS_LIVE_OPENAI_KEY")
    if not key:
        say("  skipped: JOBS_LIVE_OPENAI_KEY is not set")
        return
    from app.jobs import tailor
    from app.jobs.llm import LLM
    from test_jobs_tailor import PROFILE, REACHABLE, UNREACHABLE

    base = os.environ.get("JOBS_LIVE_OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = os.environ.get("JOBS_LIVE_CHAT_MODEL", "openai/gpt-4o-mini" if "openrouter" in base else "gpt-4o-mini")
    llm = LLM(api_key=key, base_url=base, model=model, attempts=3, timeout=120, max_tokens=2500)
    say("  model %s through %s" % (model, base))
    for label, job in (("a job the facts cover", {**REACHABLE, "technologies": REACHABLE["technologies"] + ["qdrant", "n8n"]}), ("a job that asks for Kubernetes, dbt and Kafka", UNREACHABLE)):
        t0 = time.monotonic()
        res = tailor.tailor(PROFILE, job, llm, threshold=90, max_rounds=4)
        say("  %s: score %d (ceiling %d), %d rounds, grounded %s, reached 90: %s, model used: %s, %.0f s" % (label, res.score, res.ceiling, res.rounds, res.grounded,
                                                                                                      res.reached_threshold, res.used_model, time.monotonic() - t0))
        for h in res.history:
            say("     round %d: score %d, grounded %s%s" % (h.n, h.score, h.grounded, (", rejected for: " + ", ".join(h.ungrounded)) if h.ungrounded else ""))
        say("     unsupported terms kept out: %s" % (res.unsupported or "none"))
        say("     leaked an unsupported term into the document: %s" % any(t in res.resume_text.lower() for t in res.unsupported))
        say("     the cover letter has %d words and %d ungrounded tokens" % (len(res.cover_letter.split()), len(tailor.ungrounded(res.cover_letter, PROFILE, "%s %s" % (job.get("company_name"), job.get("title"))))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    args = ap.parse_args()
    try:
        upstash()
        embeddings()
        tailoring()
    finally:
        text = "\n".join(out)
        for secret in (os.environ.get("JOBS_LIVE_OPENAI_KEY"), os.environ.get("UPSTASH_MANAGEMENT_API_KEY")):
            if secret:
                text = text.replace(secret, "<key>")
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
