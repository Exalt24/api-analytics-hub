# Railway

`railway.json` at the root of the repository is Railway's config as code for the API service: the builder, the start command, the health check and the restart policy. It is valid against the JSON Schema Railway publishes at https://railway.com/railway.schema.json (a saved copy is `deploy/railway/railway.schema.json`, read on 2026-11-07), and `tests/test_deploy_railway.py` checks that, that the start command names a module and an object that exist, and that the health check path is a route that does not use the database.

```json
{
  "build": { "builder": "NIXPACKS" },
  "deploy": {
    "startCommand": "uvicorn app.api.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 5
  }
}
```

## What is and is not done

Written and checked against the schema: yes. Deployed to Railway: no. The Railway account behind this repository has used its one time trial (the dashboard reads "Trial expired", with no projects), and running anything again needs the Hobby plan, which is a purchase. Nothing here has been started on Railway, so the start command, the port and the health check are unconfirmed on the platform itself.

## Choices, and what is not confirmed

- **The health check is `/health`, not `/ready`.** `/health` answers 200 without touching Postgres. A check that queries the database turns a slow database into a restart loop; `/ready` does query it and is for a person or a monitor, not for the restart trigger. The test refuses the swap.
- **`$PORT`.** The service must listen on the port the platform gives it. UNVERIFIED on the pages read: the config as code reference lists the fields above but this repository has not run on Railway to show the platform sets `PORT` for this builder.
- **Builder.** `NIXPACKS` is one of the five values the schema lists; Railway also lists `RAILPACK` as experimental. Nixpacks finds the Python version and the requirements itself. UNVERIFIED: that it builds this repository's pinned requirements; no build was run.
- **Database.** The migrations need the `vector` extension (migration 010), so a plain Postgres service would stop at that file. A pgvector image as a second service, or a database that offers the extension, is a deploy step that has not been tried.
- **Secrets.** `railway.json` carries none, and a test fails if a word like password or token appears in it. Keys go in the service's variables ([secrets](secrets.md)).
- **Cost.** Railway lists the Hobby plan at $5 a month with $5 of usage included (https://docs.railway.com/reference/pricing/plans, read on 2026-11-07). That is the cheapest way to run this once.
