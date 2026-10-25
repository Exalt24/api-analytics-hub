# Monthly report

The report stage reads the dated snapshots the sync already stored for one connection, works out a month's KPI table and the change on the month before in plain code, has a writer put the narrative into words from numbers that passed a check, and saves a PowerPoint deck. It reads stored rows only and never calls a vendor API, the same rule the dashboard follows.

## Run it

```bash
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
./.venv/Scripts/python.exe scripts/monthly_report.py --month 2026-10 --tenant <tenant uuid> --dump
```

`--connection <uuid>` is needed when a tenant has more than one active connection (two would be added into one doubled total, so the command refuses instead). `--out` names the deck, `--no-model` forces the template narrative, `--data-note` prints a line on the title slide, and `--as-of YYYY-MM-DD` treats that date as today. Exit 0 means a deck was written. Exit 2 means the report could not be produced, with the reason on stderr.

The narrative is written by a model only when `GROQ_API_KEY` or `LLM_API_KEY` is set in the environment (the provider and model default to the Groq values in `app/ai/catalog_copy.py`) and `--no-model` is absent. With no key the template writes it and the insights slide says so.

## The pieces

| File | Job |
|---|---|
| `app/report/monthly.py` | Every number: day accounting, totals, month over month change, formatting. No database, no model, no slide library. |
| `app/report/insights.py` | The narrative: the template stand in, the model writer, and the grounding check both pass through. |
| `app/report/deck.py` | The PowerPoint deck, and `dump_deck`, which reads a saved deck back as text. |
| `app/report/service.py` | The only database code: reads `metric_snapshot` and `sync_run` as the request role, so row level security applies. |
| `app/report/demo_data.py` | Invented orders for the sample, with holes laid out on purpose. |
| `scripts/monthly_report.py` | The command above. |
| `scripts/build_sample_report.py` | Rebuilds the sample deck through the same code path in a scratch database. |

## Days: data, quiet, missing

The Shopify connector writes a row only for a day that had orders. So a day without rows is one of two different things, and the report keeps them apart:

* **data**: the day has at least one snapshot row.
* **quiet**: no row, and a sync run that succeeded (or finished `partial`) has a window covering the day. The sync looked and found no orders. It adds zero.
* **missing**: no row and no such run covers the day. Nobody looked. A failed run is not coverage.

Sync windows are half open, so a run ending on the 10th does not cover the 10th. Totals add the days that have data. A month with a missing day is understated by it, so its change figures carry the note "partial data", the line charts leave a gap (never a zero) on that day, and the data health slide lists the dates. A month that is not over yet only expects the days that are over, and says how many it included.

A previous month that was synced and found nothing is a **zero baseline**: the change in units is shown, the percentage is "n/a". A previous month with no rows and no run is **no previous month**, a different fact, and the table says that instead.

## The KPI table

| KPI | How it is computed |
|---|---|
| Orders, Gross sales, Refunds, Net sales | The sum of the daily values over the days that have data. Money stays an integer in minor units and is formatted exactly (Decimal, zero decimal currencies such as JPY are not scaled). |
| Average order value | Gross sales divided by orders for the month, integer division as the connector does per day. "n/a" when the month has no orders. |
| Buyers per active day | The mean of the daily distinct buyer counts, one decimal. Distinct counts cannot be summed across days, so there is no monthly total. "n/a" (never zero) when the connection withheld the metric. |
| Change | This month minus the previous month, in the metric's own unit. |
| Change % | Change divided by the absolute previous value, one decimal, rounded half up. "n/a" for a zero baseline. A rounded `-0.0` prints as `0.0%`. |

## The narrative and the grounding rule

`report_facts` reduces the report to a flat set of facts using the very strings the deck prints. Two writers turn them into lines:

* The **template** is fixed wording filled from those strings. It is used when no model key is set, and as the fallback below. The insights slide says "Written by a fixed template".
* The **model writer** reuses the catalogue copywriter's transport and error handling (`CatalogCopyWriter._complete`, now taking a system prompt) with a report prompt. It is sent the facts, never a raw row.

Either way every number in the text must be one of the facts. The check runs the catalogue check (`verify_grounded`, a substring test) and a stricter one on whole numbers, because a substring test passes a made up "41" whenever a fact contains "412". Number words such as "five" are refused too. If a model reply has an invented number, more than six lines, an exclamation mark, or no text, or the provider errors, the whole reply is dropped and the template is used, and the slide label names the reason. The template is checked by the same function, and the build fails if it ever quotes a number that is not a fact.

What is checked is numbers. A model could still write "up" about a figure that fell, or give a cause. The prompt forbids both and no code verifies it, which is why the slide names the writer and the KPI table carries the true direction.

## The deck

Seven slides: title, KPI summary table, net sales by day (this month against last month), orders by day, order value and buyers with the three best days, insights, data health. Every table cell and title is a string `monthly.py` formatted; the deck does no arithmetic. The charts are native PowerPoint charts, so the values can be read back with python-pptx.

## Sample deck

Built on 2026-10-25 from invented orders (fixed seed, no store, no customer, no client), through the same code path as the command, in a scratch Postgres database that is dropped afterwards:

* [report_2026-10.pptx](monthly-report-sample/report_2026-10.pptx), the deck
* [contact-sheet.png](monthly-report-sample/contact-sheet.png) and `slide-1.png` to `slide-7.png`, rendered with LibreOffice and pdftoppm
* [report_2026-10.txt](monthly-report-sample/report_2026-10.txt), every slide read back through python-pptx

![All seven slides of the sample deck on one sheet](monthly-report-sample/contact-sheet.png)

What the sample shows: October 2026 had 607 orders and net sales of USD 40,249.02, down 1.8% on September's USD 40,966.11. 28 of 31 days have data, 1 day is quiet, and 2 days (2026-10-14 and 2026-10-15) are missing because only a failed run touched them, so every October change is marked partial data. The sample was made with no model key, so the template wrote the insights and the slide says so.

Rebuild it with `./.venv/Scripts/python.exe scripts/build_sample_report.py` (needs the compose Postgres, or `ANALYTICS_TEST_DSN`). If `soffice` or `pdftoppm` is not found the script says so and leaves the text read back as the proof. LibreOffice paginates and spaces text a little differently from PowerPoint, and this deck was only looked at through LibreOffice.

## Tests and guards

```bash
./.venv/Scripts/python.exe -m pytest tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_cli.py tests/test_report_service_db.py -q
./.venv/Scripts/python.exe tests/mutation_check.py --only report,insights,deck
```

* `test_monthly_report.py`: the KPI table against hand computed figures (`tests/report_fixtures.py`), an empty month, a missing day, a gap in the middle, a failed run, the first month, a zero baseline, a month in progress, month and year boundaries including a leap February, percent rounding, negative months, and the refusals (two rows for a day, two currencies, a float, money with no currency).
* `test_report_insights.py`: the grounding rule from both sides, the template over seven shapes of month, and the model path through a mocked transport (grounded reply used, invented number dropped, empty, overlong and shouting replies, provider error, no writer).
* `test_report_deck.py`: the saved deck read back through python-pptx: slide count, titles, table cells, chart series with gaps, the insights and data health slides, alt text, no empty placeholder, no library author.
* `test_report_service_db.py`: against Postgres as the request role, a second tenant's rows never appear, a failed run is not coverage, and the command line writes a deck, exits 2 with the reason, and never prints a model key.
* Eleven mutation guards in `tests/mutation_check.py` break the day accounting, the zero baseline, the month boundary, the sign of a change, the duplicate refusal, the previous month check, the chart gap, the whole number grounding check, the use of refused model text, the line cap and the data health list, and require the report suite to go red.

## Limits

* One connector: a Shopify store. There is no second marketing platform, so nothing here combines ad spend or traffic with sales. A second connector is the next step named in the README.
* The sample is invented orders in a scratch database. No real store or live client went through this stage.
* The model path is tested only through a mocked transport. It was not run against a live model in this build, so how a real model behaves on the report prompt is unmeasured.
* Grounding checks numbers, not direction words or causes.
* One connection and one currency per report. Money rows with two currencies in a month are refused, not converted.
* Days follow the connector's UTC bucketing.
* The deck is a fixed layout of seven slides with no template or branding option, and no delivery: nothing emails it or runs it monthly.
* The catalogue copywriter's own substring check is unchanged. Only the report uses the stricter whole number check.
