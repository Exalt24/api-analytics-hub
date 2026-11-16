# Time tracking and Microsoft 365: the Harvest and Microsoft Graph connectors

Harvest and Microsoft Graph are written from the vendors' public documentation and ran against stand-in servers that answer in each vendor's documented shape; neither was run against a live account or tenant. Both sit behind the same connector contract, the registry and the sync runner as the others, and their metrics are migration 018. What each page says, and what it does not, is in [HARVEST_GRAPH_SOURCES.md](HARVEST_GRAPH_SOURCES.md).

## Harvest

`app/connectors/harvest.py` reads time entries into four daily figures: hours tracked and billable hours as whole seconds, entries logged, and the billable value (rounded hours times rate, in the connection's currency).

- It follows `links.next` as given and never leaves the Harvest host, so the token cannot be sent elsewhere.
- It counts its own requests and stops before the 101st in 15 seconds (the documented limit), and waits for a short `Retry-After` and hands a long one to the runner.
- It refuses an entry on a project in another currency than the connection's, never adding the two.
- A key that may not read projects still gets the three hour counts; `degraded_metrics` lists the amount as left out.
- `budget_alerts` turns the projects' hour budgets and the hours spent into `under`, `warn`, `over` or `no_hours_budget` per project. A budget that resets every month is reported as `monthly_not_checked` rather than compared with hours counted over another span.

## Microsoft Graph

`app/connectors/msgraph.py` signs in as the app (client credentials, application permissions) and reads three things.

- A SharePoint list used as a project register, into rows created and modified per day (`Sites.Read.All`).
- One Teams channel, into top level messages per day (`ChannelMessage.Read.All`). The channel is sorted by the last change of a reply chain, so every page is read and the days are counted from `createdDateTime`.
- An Excel register, with `workbook_rows`. The workbook endpoints take no application permission, so it downloads the `.xlsx` through the documented `content` call, fetches the pre-authenticated address without the token, and reads the sheet or table with openpyxl, keyed by the header row (`Files.Read.All`). A blank or repeated header is refused, because a dict would silently drop a column.

The token is renewed a minute before it expires and on a 401, once, behind a lock. A refused app registration is a failed run that says a person must fix it, with no secret in the message.

## Run it

```bash
python scripts/sync_harvest.py --tenant <tenant uuid> --connection <connection uuid>     # HARVEST_* in the environment
python scripts/sync_msgraph.py --tenant <tenant uuid> --connection <connection uuid>     # MSGRAPH_* in the environment
```

Each script's docstring lists its environment variables. Both reuse `app/sync/cli.py`, so the run is claimed with a lease, written as dated snapshots, and refused when the account in the environment is not the one the connection holds.

## Tests

```bash
python -m pytest tests/test_harvest_connector.py tests/test_msgraph_connector.py -q        # the connectors against their stand-ins, no database
python -m pytest tests/test_harvest_graph_sync_db.py -q                                   # through the sync runner into Postgres (docker compose up -d)
python tests/mutation_check_harvest_graph.py                                              # 84 guards broken one at a time; edits source, so use a copy
```

Each refusal test runs a positive control first (the same call without the defect succeeds). The two connector files also run 40 seeded worlds each against a brute force oracle that shares no code with the connector: random entries, rows and messages across window edges, zones, page sizes and transient faults, and the totals must match.
