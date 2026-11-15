# Lakehouse: a PySpark and Delta Lake medallion pipeline over the hub's daily figures

`lakehouse/` reads the hub's own daily figures, lands them untouched, cleans them into typed tables, builds a dimensional model and two reporting tables, and writes a run log and data quality flags on every run. It runs on **open source Spark and Delta Lake on one machine**. It was never run on Databricks, and it uses none of Databricks' own products (no Unity Catalog, no managed pipelines).

Built and tested against: a local Spark session, seeded sample figures written through the hub's own Postgres migrations, and a stand-in REST server written from Salesforce's documentation. Operating instructions: [lakehouse/RUNBOOK.md](../lakehouse/RUNBOOK.md).

## What is in it

```
Postgres (JDBC)      tenant, platform_connection, metric, metric_snapshot ──┐
Salesforce style     Lead records, paged JSON over HTTP ───────────────────┤
REST (stand-in)                                                             ▼
bronze_pg_*, bronze_sf_lead     raw payload as sent + source, run id, time, row hash; append only; unreadable records to bronze_quarantine
silver_tenant, silver_platform_connection, silver_metric, silver_daily_figure, silver_lead
                                typed, one row per business key, Delta MERGE upsert; unreadable rows to silver_quarantine with the reason
gold_dim_tenant, gold_dim_source, gold_dim_metric, gold_dim_date, gold_fact_daily_figure,
gold_rpt_daily_store_and_ad_spend, gold_rpt_data_freshness          one Spark SQL file each, in lakehouse/sql/
ops_run_log, ops_quality_flags, ops_checkpoint
```

**Naming standard.** `bronze_<source>_<entity>`, `silver_<entity>`, `gold_dim_`, `gold_fact_` and `gold_rpt_<what>`, `ops_<what>` for the three tables that belong to no layer. snake_case everywhere; a test checks every table in a built warehouse against the pattern.

**Why two sources.** The hub's Postgres holds the daily figures its connectors synced (a store, an ad account, a website). Salesforce leads are records, not daily figures, so they arrive through the REST path and gold turns them into daily counts (`sf_leads_created`, `sf_leads_converted`) beside the rest, joined to the source account through the org id.

## Ingestion

**Postgres over JDBC** is the main route: `spark.read.format("jdbc")` with the PostgreSQL driver fetched through `spark.jars.packages`, an explicit column list (`credentials_enc` and the raw JSONB are never selected), and an incremental read of `metric_snapshot` on `synced_at` with 48 hours of lookback. The row hash drops what was already landed. **The CSV route** (`--source csv`) reads an export of the same four tables; it exists so the stage tests and a machine with no Postgres need no database. CI runs both: the JDBC test against a service Postgres.

**REST.** `rest_client.py` is a standard library reader for the Salesforce query endpoint: bearer token, `Sforce-Query-Options: batchSize`, each `nextRecordsUrl` followed as given, an address that is not a path under `/services/data/` refused (the token never goes to another host), a repeated address refused, `totalSize` checked against what arrived, a bounded retry on 5xx. `stand_in.py` is a **stand-in** written from the same documentation as the hub's Salesforce connector ([sources](AD_AND_BUSINESS_SOURCES.md)); it is not Salesforce and no org was used. It can serve a corrupt page, a run of 503s, an overlapping page, a next address off the API or a wrong total, which the tests use. A page that cannot be read fails the ingest step and is kept in `bronze_quarantine`; what arrived before it is not landed.

**Bronze** keeps `source_name`, `source_table`, `source_account`, `ingest_run_id`, `ingested_at`, `row_hash` (SHA-256 of the payload) and `raw_payload`. It only appends. A payload whose hash is already in the table is counted as `skipped_known`, so `rows read = landed + skipped_known + quarantined` always.

## Silver

Each bronze run is taken once (`ops_checkpoint`). Every bronze row ends in exactly one of six places, counted in the run log: `inserted`, `updated` (a newer revision with a changed value, by Delta `MERGE`), `unchanged`, `stale` (an older copy arriving late, ignored), `duplicate_in_batch` (newest revision wins) and `quarantined`. A seeded property test checks that the six add up to the rows read.

**Schema evolution** was tested for the three cases. A column the contract does not know, in a row that has every column it does know, **flows through** as a new string column (Delta schema auto merge; older rows hold null). A column renamed away shows up as a missing required column and the row is **quarantined** with `missing_required:<column>; unexpected columns: <new name>`. A value that cannot be read as its type (`'12.5'` for a bigint, `2026-13-45` for a date, a timestamp that is not ISO 8601 in shape, a text that is not a UUID) is **quarantined** with `retyped:<column> is not a <type>, got '<value>'`. Nothing becomes a null silently.

## Gold

Seven Spark SQL files in `lakehouse/sql/`, run in order: three dimensions, a date dimension without gaps, the fact (grain: source account, day, metric; money in minor units with its currency beside it), the daily store and ad spend report (one row per tenant and day; the ratio is only worked out when both sides are in the same currency), and the freshness view (per source: last day, lag in days, `FRESH`, `STALE` or `NO_DATA` against the stated limit, 2 days, which is inclusive). A table is replaced only when its content differs, so a rerun on unchanged input adds no Delta version.

## Reliability

**Run log.** `ops_run_log`: run id, step, rows read, rows written, rows quarantined, started, ended, seconds, status, error and a detail column with the per-outcome counts. The rows of a run are held in memory and written in one Delta commit when the run ends, whatever happened (a killed process loses that run's rows; the data tables are unaffected). A failed step is written `FAILED` with its error text, every step after it `SKIPPED` naming the failed one, and nothing earlier is undone.

**Quality flags.** `ops_quality_flags`, one row per table and check per run (not null, unique key, accepted range per metric, known metric, freshness, silver to gold reconciliation, orphan keys, lead dates), `PASS` or `FAIL` with the counts. `python -m lakehouse.run --check` exits 2 when a key table fails. Meanings and who to tell: the [runbook](../lakehouse/RUNBOOK.md).

**Idempotent reruns.** Bronze skips known hashes, silver skips taken runs, gold skips unchanged content. Bronze, silver and gold add no Delta version on a second run on the same input; only the run log and the quality flags grow.

**Failure.** `--fail-at <step>` raises an error at a step. The tests run a failing gold step and a corrupt REST page and check the run log, that earlier layers hold their rows and versions, and that the next run finishes the work.

## Tests

Pytest for each stage with one session scoped local Spark session, a seeded property test (random batches of duplicates, nulls, bad types, impossible dates, a renamed column, non object records, older copies and replays of earlier runs, over three runs, compared key for key with a plain Python model of the rules), and a mutation harness that breaks 24 rules one at a time in a private copy and demands that a test named for each turns red. The harness follows `tests/mutation_check_salesforce.py`; it never edits the working tree.

## Measured results

One machine, one day: 2026-11-15, an AMD Ryzen 7 9800X3D (16 threads, 61 GB RAM), Windows 11, Spark 4.1.1, Delta Lake 4.3.1, Postgres 17 in Docker. Other programs were running on the machine, so a clock number is what happened here, not a guarantee.

**Tests.** `python -m pytest lakehouse/tests -q -n 4 --dist loadfile`: 68 passed in 4 minutes 31 seconds. The seeded property test ran 3 seeds of 3 runs each against the plain Python model. It found one real defect while it was being written: Spark's own timestamp parser read `2026-10-05T05:0:00` and a bare date as timestamps, so silver kept rows the contract says to quarantine; silver now checks the shape first and the case is a test and a property generator branch. **Mutation checks:** `python -m lakehouse.tests.mutation_check --jobs 3`: 24 of 24 mutants caught (each must turn a test red that is named for it): dedupe removed, quarantine removed (silver and bronze), merge made an append, stale guard removed, unchanged check removed, retype turned into a null, renamed column accepted, checkpoint not written, known hashes not skipped, identical rows landed twice, uniqueness check skipped, freshness check dropped, reconciliation broken, orphan key check dropped, range check dropped, `--check` always exits zero, gold rewritten on every run, failure logged as success, REST total not checked, REST next address not checked, schema auto merge off, lead counts off by one, freshness limit made exclusive.

**Idempotent rerun, quarantine and a failed step** ([full output](test-runs/2026-11-15-lakehouse-proof.txt), from `python -m lakehouse.proof` against the Postgres over JDBC and the stand-in REST server):

- First run: 18 steps, 2,520 daily figures and 144 leads in, 2,614 fact rows, 23 quality flags, 23 PASS.
- The same input again: every bronze, silver and gold table kept its Delta version and its row count (for example `gold_fact_daily_figure` v0, 2,614 rows, before and after); no ingest, silver or gold step wrote a row.
- A messy batch of 11 records: bronze landed 8, skipped 1 (the same bytes twice) and quarantined 2 (`not valid JSON`, `JSON but not an object`), so 11 = 8 + 1 + 2. Of the 8 landed, silver counted 2 updated, 1 stale, 1 duplicate in batch and 4 quarantined (`missing_required:metric_key`, `missing_required:value_numeric; unexpected columns: value`, `retyped:observed_on is not a date, got '2026-13-45'`, `retyped:value_numeric is not a bigint, got '12.5'`), so 8 = 2 + 1 + 1 + 4; the row with an added column made `channel` a column of `silver_daily_figure`.
- A failed step (`gold_fact_daily_figure`, injected after one new day of figures arrived): run status FAILED, exit code 1, the three steps after it SKIPPED, bronze 2,528 to 2,529 rows and silver 2,520 to 2,521 rows kept, the fact still 2,614 rows. The next run: bronze and silver unchanged, the fact 2,615 rows.

**Query tuning** ([raw output](test-runs/2026-11-15-lakehouse-tuning.json), `python -m lakehouse.tuning --rows 4000000 --runs 3`). A generated table of 4,000,000 rows (200 tenants, 730 days, 12 metrics, about 300 MB), one query for one tenant's daily totals over one month joined to the metric dimension, 61 result rows, identical in every version. One untimed warm up run, then 3 timed runs, wall clock around the whole collect, adaptive execution off and broadcast joins off (the starting point is slow on purpose):

| Version | Files read | Runs (s) | Median (s) |
|---|---|---|---|
| A baseline: 200 small files, no order | 200 | 0.723, 0.651, 0.502 | 0.651 |
| B partitioned by month | 9 | 0.278, 0.334, 0.272 | 0.278 |
| C B plus `OPTIMIZE ... ZORDER BY (tenant_id)` (4 MB target file size, 9.6 s to run) | 1 | 0.330, 0.292, 0.281 | 0.292 |
| D C plus a `BROADCAST` hint on the dimension | 1 | 0.228, 0.262, 0.225 | 0.228 |

Partitioning took the median from 0.651 s to 0.278 s and the files read from 200 to 9. Z-ordering cut the files read from 9 to 1 and did not change the time (0.292 s against 0.278 s, inside the spread of the runs): on this table and this machine the scan was already cheap, so skipping files saves little. The broadcast hint took it to 0.228 s. Overall 0.651 s to 0.228 s, a little under three times. A query this small does not show what these changes would do on a table of hundreds of gigabytes, and none of this was measured on one.

**Compute cost.** `python -m lakehouse.costs` ranks the jobs of the run log by seconds. In the proof run the quality checks were the most expensive job (6.9 s per run, three runs 20.8 s), then `silver_daily_figure` (1.7 s per run on average) and `gold_dim_date`; the table is in the proof output.

## Limits

- Local, single machine, local file system. No cluster, no object store, no Hive metastore: tables are Delta folders registered in the session's catalog by `warehouse.py`.
- The sources are sample figures and a stand-in server. A real Salesforce org was not used; a few details (the form of the next page address, the status of an error) are the stand-in's own choices and are marked in its docstring.
- Not run on Databricks. Open source Delta Lake 4.3.1 on Spark 4.1.1 (the newest pair the Delta release table lists; the delta-spark wheel itself asks for pyspark up to 4.1.1).
- On Windows Hadoop's file layer needs `winutils.exe` and `hadoop.dll` ([lakehouse/setup_windows.py](../lakehouse/setup_windows.py) fetches third party builds of Hadoop 3.4.0 and checks their SHA-256). Linux and CI need nothing. PySpark's Python worker processes crashed on Python 3.13 on Windows in this setup, so the pipeline does no Python UDF and builds its small frames on the JVM side (`frames.py`); every step is JVM work driven from Python.
- Compute cost tracking is wall clock seconds and rows per job in the run log and a ranking report. It is not a cloud bill.
- Silver compares extra (added) columns only when the batch brings them; a revision from a batch that lacks an added column leaves that column's old value.
