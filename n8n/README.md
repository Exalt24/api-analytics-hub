# The monthly run in n8n

`monthly_report.workflow.json` is an n8n workflow that builds last month's report deck for each tenant on the first of the month, with a failed tenant never stopping the others and a visible alert when one fails. It was imported and executed in a real n8n container, not simulated: [docs/test-runs/2026-10-26-n8n.txt](../docs/test-runs/2026-10-26-n8n.txt) is the output of `verify_in_docker.py`, read node by node.

## Import it

In n8n, Workflows, Import from file, pick `monthly_report.workflow.json`. Or in a container: `n8n import:workflow --input=monthly_report.workflow.json`. The file is written by `build_workflow.py` and a test checks that the committed file is what the script writes, so edit the script and run it, not the JSON.

## What it needs

The workflow reads its settings from environment variables, and n8n only lets an expression read them when `N8N_BLOCK_ENV_ACCESS_IN_NODE` is `false`. Set that on the n8n service.

| Variable | Meaning |
|---|---|
| `HUB_BASE_URL` | where the hub API is, for example `https://hub.example.com` |
| `HUB_TENANTS_JSON` | the tenants to report on: `[{"name": "Example Outfitters", "api_key": "..."}]`. The key decides the tenant, the hub never takes a tenant from the request. Give each key the operator role. An entry may also carry `connection_id`, `web_connection_id`, `paid_connection_id` and `search_connection_id`, each optional, which go into the request body as written: a client with two active ad accounts needs `paid_connection_id`, or the hub answers 422 and that client gets no deck. An entry without them sends the body it always sent |
| `HUB_REPORT_MONTH` | optional `YYYY-MM`, for a backfill. Default: the month before the run |
| `HUB_AS_OF` | optional `YYYY-MM-DD`, treated as today when the report is built (a backfill or a demo) |
| `DELIVER_SLACK` | `true` posts a summary for each tenant to Slack. Anything else leaves Slack off |
| `SLACK_WEBHOOK_URL` | an incoming webhook URL, used only when Slack is on |
| `DELIVER_EMAIL` | `true` emails the deck. Anything else leaves email off |
| `EMAIL_FROM`, `EMAIL_TO` | the addresses, used only when email is on. The Send Email node needs an SMTP credential set in n8n, which is never committed |

Delivery is **off** until a toggle is `true`, so the workflow runs to the end with only the hub's own API keys: the decks are built and kept by the hub, and each tenant's result reads "built, delivery off".

It uses no Code node and no Execute Command node. n8n 2.x disables the Execute Command node by default and runs a Code node in a task runner, and a stock container may have neither, so everything is an HTTP Request to the hub, a Set, an IF, a Merge or an Aggregate.

## What it does

The Schedule Trigger fires on `0 13 1 * *`, 13:00 UTC on the first of every month. Run now does the same by hand. The hour is chosen for the data: the ads, search and website syncs end their default window twelve hours behind UTC, so 31 October is stored only by a sync that runs at or after 12:00 UTC on 1 November. From 12:00 to 23:59 the sync ends on the 1st, the whole of the month and none of the next one, and 13:00 leaves an hour after that edge. A schedule before 12:00 builds a deck a day short for those three sources, and because the deck is kept under its key it is never replaced. So run each source's sync after 12:00 UTC on the 1st and before 13:00, or the deck is built without that last day and says so. Then:

1. Settings works out the month and reads the variables, and Split Out makes one item for each tenant.
2. Build the report calls `POST /api/reports/monthly` for each tenant with that tenant's key and an `Idempotency-Key` of `monthly-report:<tenant name>:<month>`. It retries three times, which is safe because of the key. A tenant whose call fails, because its report cannot be built or its key is wrong, goes down the node's error output and becomes a result of "failed" while the next tenant carries on.
3. A deck the hub says was already delivered is skipped. Otherwise, with a toggle on, the summary goes to Slack and the deck is fetched and emailed, then `POST .../delivered` records it, so the next call for that month sees it. A failure at any of those steps is a result of "failed" for that tenant too.
4. Every result is gathered into one list. If any is "failed", a Slack alert goes out when Slack is on, and Stop and Error ends the execution as an error naming each failed tenant and why, which is what n8n's executions list, its error workflow setting and its failure emails show.

## Safe to repeat

The hub keeps the first deck built for a key, so asking again for the same tenant and month, a minute or a month later, returns the same bytes (the same SHA-256) and builds nothing. Two calls at the same moment build one deck, and the other is told to retry. A message is sent once: the delivered flag is set only after a send, and a replay carries it, so a second run skips delivery. If a send succeeded and marking it failed, the next run sends again, so delivery is at least once and never silently dropped.

## Make and Zapier

None was built or run and no account is held. The call that this workflow makes, `POST /api/reports/monthly` with an API key and an `Idempotency-Key` header, is a plain authenticated HTTP request, so a Make scenario or a Zap would make the same one. Nothing here says more than that.

## The real run

`python n8n/verify_in_docker.py --summary out.txt` needs Docker, the `n8nio/n8n:2.44.6` image (the tag the saved runs used), the compose Postgres and the repository's Python environment. It makes a scratch database with invented tenants, starts the API and a stand-in Slack webhook on the loopback address, runs the workflow in seven throwaway containers, reads the node outputs from `n8n execute` and checks them against the database and the Slack stand-in, then removes everything it started.
