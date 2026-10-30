# Snapshot change detection

*Compare one day's file of about 400,000 carriers with what is held, keep every change forever, and raise each alert once. Built and measured on invented data, 2026-10-24.*

## What this answers

A daily job that loads a regulator style file of carriers, finds insurance and authority changes, matches them to companies already on file by USDOT or MC number, stores the history, never alerts twice for the same fact, pushes follow up tasks to a CRM and writes a daily report. This folder holds the reusable core of that: `app/changes.py`, `migrations/005_change_detection.sql`, a generator for an invented file and a measuring script. It is a lab slice, public on purpose because the data is made up and the design is smaller than a real deliverable.

**No real file, no real regulator, no CRM.** The daily file is invented: one row per carrier, names made from word lists, USDOT numbers from 90,000,000 up (a range this lab reserves for itself), MC numbers from 9,000,000 up, insurers that do not exist. The alert sink is a plain callable, and the one used here is a fake that records its calls and creates one task per external key. Nothing in this folder reads data from the Federal Motor Carrier Safety Administration or any other agency.

## Measured run

Machine: a desktop Windows 11 PC (AMD Ryzen 7 9800X3D, 8 cores and 16 threads, 64 GB of RAM, an NVMe disk), not a laptop. Postgres 17.11 in a Docker container, local, default settings, other containers and a few other programs running at the same time. The data is 400,000 invented carriers on day 1 and 400,200 on day 2 (files of about 49 MB each), with 0.5 percent of carriers changed on day 2 (an assumption, not a measured rate of any real file), 200 new carriers and 800 name only edits that must raise nothing. One run on one machine: treat the seconds as a size of the thing, not as a benchmark. An earlier run of the same command gave 30.51 and 9.38 seconds for the whole day, so expect a spread of a few percent from run to run. The COPY stage is slower than a bare COPY because the staging table cleans every cell as it lands, and the machine was in ordinary use (other containers and programs open).

Command: `python scripts/measure_change_detection.py --dir <folder> --json-out docs/change-detection-measurement.json`. Generating both files took 10.21 seconds and is not counted below. Every number on this page is read from that saved file.

| Stage, in seconds | Day 1 (baseline, 400,000 rows) | Day 2 (400,200 rows) |
|---|---|---|
| COPY into the unlogged staging table (cells cleaned as they land) | 7.09 | 8.74 |
| reject, deduplicate, count unknown statuses, size check | 0.57 | 0.54 |
| one set based compare on the hash, plus the held-but-absent count | 0.38 | 0.39 |
| history rows for every changed carrier | 8.44 | 0.04 |
| events, company matches, outbox rows | 0.00 | 0.08 |
| update and insert the current table | 14.33 | 0.08 |
| **whole day, one transaction** | **30.81** | **9.89** |

Day 1 is the baseline: it fills the tables and raises no alert. On day 2, 2,200 rows were examined out of 400,200 (the hash moved or the carrier was new), 0 held carriers were absent from the file, and 2,371 events were raised.

Events by kind on day 2, checked against a manifest the generator wrote with its own plain Python rules (they matched: True):

| Kind | Events |
|---|---|
| authority_status_changed | 321 |
| cancellation_filed | 464 |
| insurer_changed | 1,046 |
| new_entity | 200 |
| replacement_filed | 340 |

The file for day 2 carried 400,000 rows the day before and the report shows that comparison, so a file that shrinks a little each day is visible long before it trips the floor below. Of the events, 39 matched exactly one company, 2 matched two companies (the USDOT number points at one and the MC number at another, so a person decides) and 2,330 matched none. Only the matched and conflicting events are queued for the CRM: 41 alerts went to the fake sink in 0.09 seconds (0.087 in the saved file), one task each (41 tasks).

**Day 2 again.** The same file loaded a second time examined 0 rows and raised 0 events (the table held 2,371 events before and 2,371 after, and every other table was identical). It took 8.03 seconds, almost all of it the COPY.

**A crash inside the transaction.** The load was made to raise an error at each stage in turn, on a second database holding day 1, and the tables were fingerprinted before and after (row counts and a hash over the whole current table):

| Crash after stage | Result |
|---|---|
| setup | tables unchanged |
| copy | tables unchanged |
| clean | tables unchanged |
| compare | tables unchanged |
| history | tables unchanged |
| events | tables unchanged |
| apply | tables unchanged |

A clean load after those 7 crashes raised 2,371 events, the same counts by kind as the first run (True), and loading the file again raised 0.

**Cancelled, reinstated, cancelled again.** 100 carriers cancelled on day 2 were reinstated on day 3 (100 rows changed, 0 events) and cancelled again on day 4 on the same filing id with a new cancel date (100 events). The two cancellations of those carriers produced 200 cancellation alerts (the scenario says 200), and loading day 4 again raised 0.

History: 402,200 history rows were held after day 2 (one per carrier on day 1 plus one per change).

The daily report for day 2, as printed by `format_report`:

```
Carrier changes for 2026-10-22
400200 rows in the file (previous load 400000, +0.1 percent), 2200 with a tracked change, 200 new carriers, 0 held carriers not in the file
  cancellation_filed         464
  replacement_filed          340
  insurer_changed            1046
  authority_status_changed   321
  new_entity                 200
alerts: 39 matched a company, 2 matched two (needs a person), 2330 matched none; 0 repeats suppressed
outbox: 0 pending, 41 delivered, 0 dead
  cancellation_filed  USDOT 90052482  Lab Account 00240
  cancellation_filed  USDOT 90254367  Lab Account 01492
  cancellation_filed  USDOT 90002216  Lab Account 00843
  cancellation_filed  USDOT 90238244  Lab Account 03189
  cancellation_filed  USDOT 90317610  Lab Account 00680
  cancellation_filed  USDOT 90073482  Lab Conflict 2 A, Lab Conflict 2 B
  cancellation_filed  USDOT 90198129  Lab Account 00222
  replacement_filed  USDOT 90371920  Lab Account 01527
  replacement_filed  USDOT 90254328  Lab Account 00981
  replacement_filed  USDOT 90078527  Lab Account 01741
```

## How it works

1. **Stage.** The file is copied with `COPY` into an unlogged staging table as text. Generated columns clean each cell in the database: whitespace trimmed and collapsed, a blank or an empty quoted cell turned into NULL, insurer and statuses upper cased, dates read only as ISO.
2. **Compare.** One statement joins staging to the current table on the normalised USDOT number and keeps a row only when its hash differs or the carrier is new. The hash covers the tracked columns only: insurer, policy status, authority status and its date, filing id, filing date and cancel date. A change to a name or an MC number alone is not examined.
3. **History.** Every examined row is written to `carrier_history`, forever. A trigger refuses an update, a delete or a truncate on it and on the events.
4. **Events.** The examined rows become events: `new_entity`, `insurer_changed`, `cancellation_filed`, `replacement_filed` and `authority_status_changed`. A row can raise several (a replacement with a new insurer raises two). Each carries a natural key (carrier, kind, reference) behind a UNIQUE constraint, and the insert is `on conflict do nothing`.
5. **Match and queue.** Events are matched to `carrier_company` by USDOT number or by MC number, after both sides are normalised. An event that matched a company gets an outbox row with its payload stored once. An event with no company is recorded and reported, and never pushed.
6. **Apply.** The current table is updated. All of it happens in one transaction, so a crash at any point leaves the database as it was, and the same file can be loaded again.
7. **Push.** `deliver_pending` claims due rows with a lease, calls the sink with the stored payload and an idempotency key, and stores the answer. A row is delivered only when the sink answered `ok`.

### What makes two alerts the same alert

| Kind | Reference in the natural key |
|---|---|
| `new_entity` | none |
| `insurer_changed` | the new filing id |
| `cancellation_filed` | the filing id and the cancel date (the filing date when there is no cancel date) |
| `replacement_filed` | the filing id and the cancel date (the filing date when there is none) |
| `authority_status_changed` | the new status and its date |

So a policy cancelled, reinstated and cancelled again on one filing id with a new cancel date alerts twice, a replay of either day alerts once, and an authority lost twice alerts twice. A carrier that flaps back to a state whose filing and dates it already had does not alert again, and every state is still kept in history. A correction that reuses a filing id for another insurer is also read as the same alert.

### Matching

A USDOT number is reduced to its digits without leading zeros. An MC number keeps a docket prefix (MC when there is none, FF for a freight forwarder, MX for Mexico) and the digits without leading zeros, so `MC-0012345`, `0012345` and `12345` are one docket and `FF-12345` is a different one. A company may hold either number or both. A carrier matched by the USDOT number of one company and the MC number of another is reported as a conflict with both named, and none is picked. A company added after an event was raised is matched with `match_late_company(conn, company_id, days=30)`, which queues the events of the last N days that match it and never a second queue row for an event already queued.

### The outbox, and what the CRM has to do

The payload is built once, when the event is raised, and every retry sends those same bytes. It carries `external_key`, the same value as the table's `idempotency_key` (carrier, kind and reference). **The key goes into a unique field on the CRM record, and the sink upserts on it.** The upsert and rate-limit developer pages of HubSpot, Zoho, Salesforce and GoHighLevel were searched for an idempotency key header and none of those pages documents one; that is absence from the pages read, not proof that a vendor has none. What three of them do document is an upsert on a unique key: Salesforce by external id (a key matched more than once returns a 300 error and the record is neither created nor updated), HubSpot by a custom unique property (`idProperty`), and Zoho by duplicate check fields ("The Upsert API allows you to insert a new record or update an existing one based on duplicate check field values"). GoHighLevel's upsert matches on email or phone under a location setting, so it cannot carry this key and is the weakest fit. Rate limits seen on those pages: HubSpot 100 requests per 10 seconds per app on Free and Starter (190 on Professional and Enterprise) with a 429 on excess, GoHighLevel a burst of 100 per 10 seconds and 200,000 per day, which is why the outbox has a `throttled` answer.

A sink answers `ok` (with its own id for the record), `rejected` (permanent: the row goes dead), `ambiguous` (it may or may not have acted), or `throttled` with an optional `retry_after`, which reschedules without spending an attempt. A raise, an answer the sink should not give and a timeout are all treated as ambiguous: the row stays pending and is retried with the same stored payload and a growing wait, until `max_attempts` and then dead. `requeue_dead` puts dead rows back with their attempts reset and the reason recorded. A claim and an outcome each commit on their own, and an outcome is stored only by the worker that still holds the claim.

**Delivery is at least once, and once in effect only when the sink upserts on `external_key`.** Two tests show both sides: a worker that dies after the sink stored the record and before the outcome was stored resends, and a fake that upserts on the key ends with one record, while a fake that only appends ends with two. No real CRM was used to build or test this.

### Absence

Whether a carrier that is missing from a file is news depends on the source. FMCSA's InsHist description says "all insurance information is related to the insurance policy either being cancelled, being replaced, or prior to a name change": an ending is a row, with a status and a cancel date. The Motus Insur description says "active or pending individual insurance policies": a policy that ends simply vanishes from the file, and there is no status column to read. No page read for this project defines the Daily Difference datasets or states their refresh schedule. The invented file here is the first shape.

So absence is a choice. By default a carrier held and missing from a file raises nothing and is counted as `rows_missing`. With `absent_is_event=True` (CLI flag `--absent-is-event`) one `dropped_from_file` event is raised the first time a carrier is missing, it is matched and queued like any event, the carrier is marked, and a mark stops a second event on the next absent day or on a rerun. A carrier that returns clears the mark through the history path with no event, and vanishing again raises a new event, because the key carries the load the carrier last changed in. In this mode the floor below cannot be skipped with `force`, since a truncated file would otherwise read as thousands of endings.

### Files that are refused

A load raises `LoadRefused` before anything commits when the file has no data rows (never overridden), when it stages under half of the carriers held, when more than a quarter of its rows moved, or when it is dated more than a day after the database's date. Both ratios are keyword parameters and `force=True` skips the three judgement calls, never the empty file. A file older than a load already applied is refused as stale and is not overridden. Carriers held but absent from a file raise no event, because a truncated file and a feed that pruned a carrier look the same; they are counted on the load as `rows_missing` and shown in the report. Policy status values outside ACTIVE, CANCELLED and REPLACED are counted on every load and shown in the report, so a change of vocabulary in a feed appears as a number.

**The 50 percent floor and the 25 percent ceiling are this project's own defaults.** They are parameters, `force` overrides them, and they are not numbers from a source: a read of Great Expectations, the Deequ paper, dbt freshness, Stripe webhooks and the AWS Builders Library supports the direction (bound a batch before it is promoted, check the schema, key an event by object and type, let `ON CONFLICT` enforce it) and gives no sourced percentage for rejecting a file. The one sourced alternative is statistical: Deequ (VLDB paper) detects anomalies in the time series of batch sizes, with a warning when a size is more than 3 standard deviations from the previous mean and an error above 4. That needs a history of loads, and this project only compares with the previous one.

### Notes on PostgreSQL behaviour this relies on

- The load lock is a transaction level advisory lock (`pg_try_advisory_xact_lock`). Session level advisory locks ignore transaction semantics and would survive a rollback.
- The staging table is unlogged, so PostgreSQL empties it after a crash. That costs nothing: it holds only the file being loaded, and a crashed load is loaded again.
- Autovacuum cannot analyse a temporary table, so the load runs `ANALYZE` on the staging table and on the temporary delta table before the joins that read them.
- Events are inserted with `ON CONFLICT DO NOTHING`, not `DO UPDATE`: `DO UPDATE` raises an error when one statement affects the same row twice, and an event that already exists must stay exactly as it was. `MERGE` exists from PostgreSQL 15, and its own documentation points to `ON CONFLICT` for concurrent inserts, which is the case here.

## What FMCSA publishes, and where this design fits

These notes are from FMCSA's own dataset pages on data.transportation.gov, read on 2026-10-24. This project did not load any of that data.

- The legacy Carrier, Insur, InsHist and AuthHist All With History datasets were last refreshed on 05/14/2026 and will no longer be updated.
- Their replacements, from the Motus system, are published both as a Daily Difference dataset and as an All With History dataset. On 2026-10-23 the Daily Difference dataset for insurance held 179 rows against about 142,000 in its history twin (read from the dataset's own metadata page by hand, not by this code).
- The insurance history file holds the old policy of a cancellation or a replacement ("It is not the subsequent (if applicable) policy"), so the new policy is a separate record; this design reads one row per carrier and does not follow that pairing.
- One USDOT number can hold several dockets, and Motus gives each authority its own docket.
- Values seen in the live rows of those pages differ from the documented codes (a cancellation reason shows as `TERM/REPL`, a form code as `BMC-84`). Those rows were read by hand for these notes; no code in this repository reads them.

What follows for this design. The daily file here is an invented one row per carrier file. A source that already publishes a daily difference does not need a compare of this size, and this design is for a source that only publishes the whole file. A source that publishes one row per filing would key the compare by carrier plus policy, not by carrier alone, and would need its own rules for a cancellation and the policy that replaces it, since the feed does not link them. Because this loader keeps the last row for a repeated carrier, it is right for a one row per carrier file only.

## Limits

- Invented data, one machine, one run of each measurement. The seconds are a size, not a benchmark, and they were taken with other programs running.
- Not the regulator's files. The change rate of 0.5 percent is an assumption. The status vocabulary and the file shape are the lab's own.
- No CRM, Slack or n8n account was used. The sink is a fake. Delivery is at least once, and once in effect only when the real sink honours the idempotency key.
- Not deployed and not scheduled. Nothing calls the load or the delivery on a clock.
- Not tenant scoped, and no row level security on these tables, unlike the tables from migrations 001 to 004.
- The unlogged staging table keeps the last file's raw text (including the legal names) until the next load truncates it, and the compose database ships a known development password on a port that is bound to the loopback address only.
- A repeated carrier in one file keeps its last row. That is wrong for a file with several rows per carrier.
- Untracked columns (the name, the MC number) refresh only when a tracked column moves.
- A carrier that disappears from a file is counted and left alone. Nothing here decides that a carrier has left the feed.
- The 0.5 percent and the 25 percent refusal threshold are judgement calls and are parameters.
- A changed insurer with no filing id is keyed by an empty reference, so only the first such change on a carrier alerts. Fill the filing id, or accept that.
- A trailing non-breaking space (U+00A0) in a text cell is not trimmed, so such an insurer name reads as a change. Ordinary spaces, tabs and quote marks are handled.
- In `--absent-is-event` mode the day after a feed outage hides many carriers, the returning carriers count as moved rows against the 25 percent ceiling and the load is refused until the operator passes `--force`.
- An empty `bytes` source raises `ValueError` instead of `LoadRefused` (nothing is written either way); an empty file path raises `LoadRefused`.
- Two loads on one date are allowed (a corrected file, a rerun): the report says how many loads the date held and takes the file numbers from the first.
- A crash was simulated by raising an error from a callback at each of 7 stages, not by killing a process or cutting a connection.
- A file that ends in a blank line is refused by COPY with `BadCopyFileFormat` (missing data for a column) and nothing is written; edit the blank line away. A semicolon delimited file is refused at the header check.
- The tests are `tests/test_change_detection.py` and two replays of independent reviewers' corpora, `tests/test_change_detection_corpus.py` (45 cases) and `tests/test_change_detection_corpus_2.py` (44 cases written against the finished build by a reviewer who had seen neither the first corpus nor any earlier finding; each case quotes the line of this page it tests and states the harm it guards against, and all 44 agreed with the code); four cases of the first corpus are marked as known limits (four items above) and are expected to keep failing until someone decides to fix them.
- The README section and these docs do not claim the module is wired into the multi-tenant hub: it is a separate slice, not scheduled and not tenant scoped.

## Run it yourself

```bash
docker compose up -d                                   # Postgres on 5433, the compose database is named analytics
# a scratch database for everything below (the tests and the measuring script drop and create things in the database they are given)
docker compose exec db psql -U analytics -d postgres -c "create database changes_scratch"
export ANALYTICS_TEST_DSN="postgresql://analytics:analytics@127.0.0.1:5433/changes_scratch"
export DATABASE_URL="$ANALYTICS_TEST_DSN"
for f in migrations/00*.sql; do psql "$DATABASE_URL" -f "$f"; done          # the loader does not migrate: nothing is there until this runs
python scripts/make_carrier_snapshot.py --out-dir <folder> --n 400000 --flappers 100    # day 1 and 2, and day 3 and 4 for the flapping carriers
python scripts/load_carrier_snapshot.py <folder>/carriers_day1.csv --date 2026-10-24    # the first load is the baseline: no alerts
python scripts/load_carrier_snapshot.py <folder>/carriers_day2.csv --date 2026-10-25    # prints the daily report
python scripts/measure_change_detection.py --dir <folder>                             # the timed run below, with a fake CRM sink and the crash tests
python -m pytest tests/test_change_detection.py -q
```

The loader loads one file and prints the report. It does not load the company book or push alerts: matching, the fake CRM and delivery are driven by `measure_change_detection.py` and by the tests, so a stranger who only runs the loader sees `0 matched a company` whatever the data. The measuring script needs `--n 3000` or more: below that no carrier flaps, so the day 3 file it reads is never written.

**The tests drop and recreate the `public` schema of whatever database `ANALYTICS_TEST_DSN` names, and its default is the compose `analytics` database, so create a scratch database first and set the variable to it before the pytest line** (anything you seeded for the dashboard in that database would be lost). The measuring script makes two scratch databases on the server named by `ANALYTICS_TEST_DSN` (the compose Postgres by default), applies migration 005 to each and drops both at the end. A 2,000 carrier version of the same flow, with a few carriers that cancel, return and cancel again, is a test in `tests/test_change_detection.py`.
