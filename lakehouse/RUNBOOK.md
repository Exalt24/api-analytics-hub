# Lakehouse runbook

How to operate the pipeline in this folder: run it, read what it says, re-run a failed day, backfill, and what each quality flag means. It is written so a person who did not build it can do all of that. Background and design: [docs/lakehouse.md](../docs/lakehouse.md).

The pipeline reads the hub's Postgres tables (JDBC) and a Salesforce style REST endpoint, lands them untouched in bronze, types and de-duplicates them into silver, builds the dimensional model and two reporting tables in gold, and writes a run log and quality flags every time. It runs on open source Spark and Delta Lake on one machine.

## Run it

```bash
python -m venv lakehouse/.venv && . lakehouse/.venv/Scripts/activate     # macOS or Linux: . lakehouse/.venv/bin/activate
pip install -r lakehouse/requirements.txt
python -m lakehouse.setup_windows                  # Windows only, once: the two files Hadoop's file layer needs (winutils.exe, hadoop.dll)
docker compose -p lakehouse-pg -f lakehouse/compose.yaml up -d --wait      # the source Postgres, on its own port 5543
python -m lakehouse.sample_data --seed 7 --days 60 --end 2026-11-01 --csv-dir lakehouse/data/export \
    --postgres postgresql://analytics:analytics@127.0.0.1:5543/postgres    # sample figures through the hub's own tables
python -m lakehouse.stand_in --batch 50            # the stand-in REST server (leave it running in another terminal)

python -m lakehouse.run --source jdbc --jdbc-url jdbc:postgresql://127.0.0.1:5543/lakehouse_src \
    --rest-url http://127.0.0.1:<port it printed> --rest-token stand-in-token --org-id 00DST0000000001AAA --as-of 2026-11-01
```

Without Docker, `--source csv` reads the CSV export instead of Postgres. Everything else is the same.

**Exit codes of `python -m lakehouse.run`:** 0 every step and every key check passed. 1 a step failed (the run log names it). 2 every step ran and a key quality check failed.

## Where things are

| What | Where |
|---|---|
| Delta tables | `lakehouse/warehouse/<table>/` (override with `--warehouse` or `LAKEHOUSE_WAREHOUSE`) |
| Run log, one row per step per run | table `ops_run_log` |
| Quality flags, one row per table and check per run | table `ops_quality_flags` |
| Rows that could not be read | `bronze_quarantine`, `silver_quarantine` (each has the reason and the raw text) |
| Which bronze runs silver has taken | `ops_checkpoint` |
| Gold SQL | `lakehouse/sql/*.sql`, one file per gold table |
| Cost report | `python -m lakehouse.costs` |

Read any table with `spark.read.format("delta").load("lakehouse/warehouse/<table>")`, or from any other Delta reader.

## The daily check

1. `python -m lakehouse.run ...` (or the scheduler that runs it). Look at the last line and the exit code.
2. Exit 0: done.
3. Anything else: open the run log for the last run.

```python
spark.read.format("delta").load("lakehouse/warehouse/ops_run_log").where("status != 'SUCCEEDED'").orderBy("started_at").show(truncate=False)
```

`FAILED` is the step that broke, with its error text. `SKIPPED` is every step after it, with the name of the step that stopped the run. Nothing the steps before it wrote is undone: bronze and silver only append and merge, and gold keeps its last good content until a later step replaces it.

## Re-run a failed day

The pipeline is safe to run again at any time. Bronze skips every payload it already holds (by hash), silver takes each bronze run once (`ops_checkpoint`), and gold is replaced only when its content differs. So:

1. Find the failed step and fix its cause (table below).
2. Run the same command again. The steps before the failure find nothing new to do; the failed step runs again; the steps after it run for the first time.
3. Check the run log for the new run: every row `SUCCEEDED`, `rows_written` 0 for the steps that had already finished.

| Failed step | Usual cause | What to do |
|---|---|---|
| `ingest_pg_*` | Postgres down, wrong URL or password | start it, check `--jdbc-url`, run again |
| `ingest_sf_lead` | the REST endpoint answered 5xx, a bad token, a page that is not JSON, a total that does not match | the error text says which. A 5xx is retried twice already; run again later. A page that is not JSON is saved to `bronze_quarantine` (the raw text) for whoever owns the endpoint. Nothing from a half read page set is landed |
| `silver_*` | an error in the merge | read the error, fix, run again; the bronze rows are still there |
| `gold_*` | a SQL file error | fix the file in `lakehouse/sql/`, run again; earlier gold tables are already rebuilt and are not rebuilt again if unchanged |
| `quality_checks` | a key check failed (run status `FAILED_QUALITY`, exit 2) | see the flags below. The data is loaded; the flag says it should not be trusted yet |

To see what a failed step looks like, run with `--fail-at <step name>`: it raises an error at that step and logs it.

## What each quality flag means

Every run writes one row per table and check to `ops_quality_flags` with `PASS` or `FAIL`, `rows_checked`, `rows_failed`, a threshold and a detail line. `python -m lakehouse.run --check` runs the checks alone against the warehouse as it is and exits 2 if a key table fails. Key tables: the five `silver_` tables, the fact and the two reports. A FAIL on a bronze table is reported but does not fail `--check`.

| Flag | It fails when | Who to tell, and what to do |
|---|---|---|
| `not_null` | a key or required column holds a null | the data engineer. A null cannot come from a normal run (a row with a missing required value is quarantined), so something wrote to the table by hand or a contract changed |
| `unique_key` | a business key appears twice | the data engineer. Silver is a MERGE on its key, so duplicates mean a table was written outside the pipeline |
| `accepted_range` | a figure is outside the range for its metric (`config.RANGES`, money in minor units) | the owner of the source. A negative order count or a spend of ten billion is a source problem; the row stays in silver and is flagged |
| `known_metric` | a figure names a metric that is not in the metric vocabulary | the owner of the hub's `metric` table: a new metric was synced before it was registered |
| `freshness` (silver) | a source account's newest day is more than 2 days behind the as-of date (`config.FRESHNESS_LIMIT_DAYS`) | the owner of that connection. The detail line names the stale sources and how many days. Check the hub's sync runs for that connection |
| `freshness` (report) | the freshness view has a source that is `STALE` or `NO_DATA` | same as above. `NO_DATA` means a connected source with no figures at all |
| `reconciliation` | silver and gold disagree on a metric's total | the data engineer. Rebuild gold; if it still fails, a SQL file drops or double counts rows (a lead whose org has no connection drops out of the fact) |
| `orphan_keys` | a fact row names a tenant, source, day or metric with no dimension row | the data engineer. A dimension table is behind or damaged: run again |
| `lead_dates` | a lead is created after the day following the as-of date, or modified before it was created | the Salesforce administrator |

A quarantined row is not a failure of the run. Read `silver_quarantine` for the reasons:

| Reason | Meaning |
|---|---|
| `missing_required:<column>` | the column is absent or empty. With `unexpected columns: <name>` after it, the source renamed the column to that name |
| `retyped:<column> is not a <type>, got '<value>'` | the value cannot be read as the column's type |
| `unsafe_column_name: <name>` | the source added a column whose name is not a plain identifier |
| `not valid JSON ...`, `not a JSON object ...` (bronze) | the record or page could not be read at all |

An added column with a plain name is not an error: it flows into silver as a new string column.

## Backfill

*A change in the source's past* (a connector corrected old days): the incremental read re-reads 48 hours before the newest `synced_at`. For older changes run once with `--full-refresh`: it reads every source row again, bronze skips what it already holds by hash, and silver merges only the rows that differ (a newer revision updates the row, an older copy is counted as `stale` and ignored).

*A new history to load* (a source added with 90 days behind it): point the run at it and run once with `--full-refresh`.

*Late arriving days* need nothing special: a day that shows up after later days is inserted, and the run log's `late_arrival` count in the silver step's `detail` says how many.

To rebuild gold alone after a SQL change, run again; bronze and silver have nothing new and only changed gold tables are rewritten.

Never edit a Delta table by hand to fix a number. Fix the source or the rule, and run again.

## Compute cost

`python -m lakehouse.costs` ranks the jobs (steps) by total seconds from the run log, with rows read and written and rows per second. Seconds are wall clock on the machine that ran them.

## Tests

```bash
python -m pytest lakehouse/tests -q                       # every stage, the property test, the JDBC read (skips without the Postgres)
python -m lakehouse.tests.mutation_check --jobs 2         # each rule broken in a private copy; every break must turn a test red
```
