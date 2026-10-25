# Monthly report

The report stage reads the dated snapshots the sync already stored for one connection, works out a month's KPI table and the change on the month before in plain code, has a writer put the narrative into words from figures that passed a check, and saves a PowerPoint deck. It reads stored rows only and never calls a vendor API, the same rule the dashboard follows.

## Run it

```bash
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
./.venv/Scripts/python.exe scripts/monthly_report.py --month 2026-10 --tenant <tenant uuid> --dump
```

`--connection <uuid>` is needed when a tenant has more than one active connection (two would be added into one doubled total, so the command refuses instead). `--out` names the deck, `--no-model` forces the template narrative, `--data-note` prints a line on the title slide, and `--as-of YYYY-MM-DD` treats that date as today. Without it, today is the UTC date, because the connector buckets every order by its UTC day. Exit 0 means a deck was written. Exit 2 means the report could not be produced, with the reason on stderr.

The narrative is written by a model only when `GROQ_API_KEY` or `LLM_API_KEY` is set in the environment (the provider and model default to the Groq values in `app/ai/catalog_copy.py`) and `--no-model` is absent. With no key the template writes it. Which writer ran, and why a model's text was refused if it was, is printed by the command, kept in the deck file's comments property and shown by `--dump`. It is not printed on any slide.

## The pieces

| File | Job |
|---|---|
| `app/report/monthly.py` | Every number: day accounting, totals, month over month change, formatting, and the shared wording of titles and caveats. No database, no model, no slide library. |
| `app/report/insights.py` | The narrative: the template stand in, the model writer, and the figure check both pass through. |
| `app/report/deck.py` | The PowerPoint deck, and `dump_deck`, which reads a saved deck back as text. |
| `app/report/service.py` | The only database code: reads `metric_snapshot` and `sync_run` as the request role, so row level security applies. |
| `app/report/demo_data.py` | Invented orders for the sample, with holes laid out on purpose. |
| `scripts/monthly_report.py` | The command above. |
| `scripts/build_sample_report.py` | Rebuilds the sample deck through the same code path in a scratch database. |

## Days: with orders, with no orders, with no data

The Shopify connector writes a row only for a day that had orders. So a day without rows is one of two different things, and the report keeps them apart. The deck says it in plain words, the code calls them data, quiet and missing:

* **Days with orders** (data): the day has at least one snapshot row.
* **Days with no orders** (quiet): no row, and a sync run that succeeded (or finished `partial`) has a window covering the day. The sync looked and found no orders. It adds zero.
* **Days with no data** (missing): no row and no such run covers the day. Nobody looked. A failed run is not coverage.

The three add up to the days in the month (or the days counted so far, for a month that is not over). Sync windows are half open, so a run ending on the 10th does not cover the 10th. Totals add the days that have orders.

A month with a day with no data is understated by it, so everything that states a change from it carries the caveat in the same sentence: the slide title ("Net sales fell 1.8% from September (October is missing 2 days)"), the Note column of the tables ("October partial: 2 days missing"), and each change sentence of the narrative. The charts show a day with no orders as a dot on the zero line and a day with no data as a break in the line with an X on the zero line, and the footnote says which are present and how many of the days are plotted. A month that is not over only counts the days that are over, and says how many.

A previous month that was synced and found nothing is a **zero baseline**: the change in units is shown, the percentage is "n/a". A previous month with no rows and no run is **no previous month**, a different fact. A baseline note and the partial data note are both kept on a figure.

## The KPI table

| KPI | How it is computed |
|---|---|
| Orders, Gross sales, Refunds, Net sales | The sum of the daily values over the days that have orders. Money stays an integer in minor units and is formatted exactly (Decimal; dollars print as `$`, other currencies keep their code, zero decimal currencies such as JPY are not scaled). |
| Average order value | Gross sales divided by orders for the month, integer division as the connector does per day. "n/a" when the month has no orders. |
| Buyers per active day | The mean of the daily distinct buyer counts, one decimal. Distinct counts cannot be summed across days, so there is no monthly total. "n/a" (never zero) when the connection withheld the metric. |
| Change | This month minus the previous month, in the metric's own unit, from the displayed figures. |
| Change % | Change divided by the absolute previous value, one decimal, rounded half up. The buyers percentage is computed from the unrounded means. "n/a" for a zero baseline. A rounded `-0.0` prints as `0.0%`. |

The words for a change: "up 8.4%" or "down 12.4%". "Unchanged" only when the two exact values are equal. A change that merely rounds to 0.0% is "essentially flat" and says by how much ("essentially flat (+$4.00)").

## The narrative and the figure check

`report_facts` reduces the report to a flat set of facts using the very strings the deck prints: dollars as `$40,249.02`, dates as `Oct 14`, months by name with no year. Two writers turn them into lines:

* The **template** is fixed wording filled from those strings. It is used when no model key is set, and as the fallback below.
* The **model writer** reuses the catalogue copywriter's transport and error handling (`CatalogCopyWriter._complete`, now taking a system prompt) with a report prompt. It is sent the facts, never a raw row.

Either way the text goes through `ungrounded_numbers`. It runs the catalogue check first (`verify_grounded`, a substring test that the catalogue copy keeps unchanged), then the report's own exact check:

* every amount, percentage, count and date must be one of the facts' displayed figures, written the same way. A year or a day number is never borrowed from a date, and a bare count in a sentence about a metric must be a day count ("2 days", "9 of 31 days");
* a comma between digits must be a valid thousands separator, so "1,4%", "6,07" and "20,15" are refused;
* a figure glued to a letter ("USD9", "x5", "Q4") is still a figure;
* number words are whole words from a fixed list (fifteen, dozen, hundreds, doubled, halved, twice and the rest) and are refused unless the facts hold the same word, so "one" is not found inside "none";
* when a sentence names a metric (net sales, orders, refunds, average order value or its alias, buyers) every figure in it must be one of that metric's own figures. "Net sales fell 27.1%" is refused because 27.1 is the refunds figure.

If a model reply fails any of this, has more than six lines or an exclamation mark, or has no text, or the provider errors, the whole reply is dropped and the template is used. The template goes through the same function, and the build fails if it ever quotes a figure that is not a fact.

**What this does not prove.** The metric check is per sentence: a sentence that names two metrics may use either one's figures, so a figure can still be attached to the wrong one of two named metrics. A sentence that names no metric only needs its figure to exist among the facts. The check does not look at direction words ("up" about a figure that fell) or causes. The prompt forbids both, and the KPI table carries the true direction.

## The deck

Seven slides: title, KPI summary table, net sales by day (this month against last month), orders by day, order value and buyers with the three best days, insights, data health. Every table cell and title is a string `monthly.py` formatted; the deck does no arithmetic. The charts are native PowerPoint charts, so the values can be read back with python-pptx. The slides use plain words (days with no data, data pull, daily figures) and no text is smaller than 11 points. The file's own properties are set: author, a creation time of the build, an application name and slide count in place of the template's, and no template thumbnail.

## Sample deck

Built on 2026-10-25 from invented orders (fixed seed, no store, no customer, no client), through the same code path as the command, in a scratch Postgres database that is dropped afterwards. The report date is set to 2026-11-02 (`--as-of`) so that October counts as a finished month. The store is called "Example Outfitters" on the title slide.

* [report_2026-10.pptx](monthly-report-sample/report_2026-10.pptx), the deck
* [contact-sheet.png](monthly-report-sample/contact-sheet.png) and `slide-1.png` to `slide-7.png`, rendered with LibreOffice and pdftoppm
* [report_2026-10.txt](monthly-report-sample/report_2026-10.txt), every slide read back through python-pptx, ending with the provenance line

![All seven slides of the sample deck on one sheet](monthly-report-sample/contact-sheet.png)

What the sample shows: October had 607 orders and net sales of $40,249.02, down 1.8% on September's $40,966.11. 28 days have orders, 1 day has none, and 2 days (Oct 14 and Oct 15) have no data because only a failed run touched them, so every October change is marked partial. The refunds figure moved most, up 27.1%. The sample was made with no model key, so the template wrote the insights.

Rebuild it with `./.venv/Scripts/python.exe scripts/build_sample_report.py` (needs the compose Postgres, or `ANALYTICS_TEST_DSN`). If `soffice` or `pdftoppm` is not found the script says so and leaves the text read back as the proof. LibreOffice paginates and spaces text a little differently from PowerPoint, and this deck was only looked at through LibreOffice.

## Tests and guards

```bash
./.venv/Scripts/python.exe -m pytest tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_cli.py tests/test_report_service_db.py -q
./.venv/Scripts/python.exe tests/mutation_check.py --only report,insights,deck
```

* `test_monthly_report.py`: the KPI table against hand computed figures (`tests/report_fixtures.py`), an empty month, a missing day, a gap in the middle, a failed run, the first month, a zero baseline, both notes kept, a month in progress, the UTC default for today, month and year boundaries including a leap February, percent rounding, the buyers percent from unrounded means, flat against unchanged, titles with and without the caveat, and the refusals (two rows for a day, two currencies, a float, money with no currency).
* `test_report_insights.py`: the figure check from both sides (exact figures, dates, a figure beside the wrong metric, commas, glued letters, number words), the template over ten shapes of month, and the model path through a mocked transport.
* `test_report_deck.py`: the saved deck read back through python-pptx: titles, table cells, notes, chart series with gaps and markers, the insights and data health slides, plain words, no ISO dates, no text under 10 points, and the file's own properties.
* `test_report_service_db.py`: against Postgres as the request role, a second tenant's rows never appear, a failed run is not coverage, and the command line writes a deck, exits 2 with the reason, and never prints a model key.
* Twenty three mutation guards in `tests/mutation_check.py` break the day accounting, the zero baseline, the month boundary, the sign of the divisor in a percent change ("the percent change divides by the signed previous value"), the duplicate refusal, the previous month check, the chart gap, the exact figure check, the metric check, the comma check, the number word check, the day number allowance, the use of refused model text, the line cap, the caveat in a title, flat against unchanged, the partial note, the buyers percent, both date defaults, the data health list, the no data marker and the file properties, and require the report suite to go red.

The saved output of the 2026-10-25 runs (the whole suite against a scratch database, before and after, and the guard runs) is in [test-runs/2026-10-25.txt](test-runs/2026-10-25.txt).

## Limits

* One connector: a Shopify store. There is no second marketing platform, so nothing here combines ad spend or traffic with sales. A second connector is the next step named in the README.
* The sample is invented orders in a scratch database. No real store or live client went through this stage.
* The model path is tested only through a mocked transport. It was not run against a live model in this build, so how a real model behaves on the report prompt is unmeasured.
* The figure check is per sentence and does not read direction words or causes (see above).
* One connection and one currency per report. Money rows with two currencies in a month are refused, not converted.
* Days follow the connector's UTC bucketing.
* The deck is a fixed layout of seven slides with no template or branding option, and no delivery: nothing emails it or runs it monthly.
* The catalogue copywriter's own substring check is unchanged. Only the report uses the exact figure check.
