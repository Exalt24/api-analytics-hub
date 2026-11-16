"""The job pipeline: vendor postings in, one row per job out, tailored documents, a guarded form filler and a human review queue.

Read docs/job-pipeline.md first. The modules, in the order a job travels:

  dedupe.py     a sliding window in Redis that says "seen in the last N hours" before the database is touched
  archive.py    the raw vendor JSON, gzipped, in S3 compatible storage (Cloudflare R2 or AWS S3) before anything else happens to it
  store.py      Postgres upserts, one row per distinct job
  embed.py      OpenAI text-embedding-3-small through LangChain into Qdrant, one vector per new job
  pipeline.py   ingest_batch(): the five steps above for one batch, safe to run twice
  tailor.py     resume and cover letter loop: ATS match score to a threshold, and a grounding check against the applicant's own facts
  formfill.py   form field mapping with a confidence per field; a field under the threshold is never typed, it becomes a review task
  browser.py    a Playwright page from local Chromium or from a Browserbase session
  hitl.py       the review queue, pause and resume, RabbitMQ messages and the callback to an n8n Wait node
"""
