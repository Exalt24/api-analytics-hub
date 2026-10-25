# Monthly report

The report stage reads the dated snapshots the sync already stored for one connection, works out a month's KPI table and the change on the month before in plain code, has a writer put the narrative into words from figures that passed a check, and saves a PowerPoint deck. It reads stored rows only and never calls a vendor API, the same rule the dashboard follows.

## Run it

Commands below assume the virtual environment from the README is active, so `python` is its interpreter (activate with `. .venv/Scripts/activate` in Git Bash on Windows, `. .venv/bin/activate` on macOS or Linux). Everything here was run on Windows 11 with Git Bash; the macOS and Linux forms are the standard venv layout and were not run.

```bash
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
python scripts/monthly_report.py --month 2026-10 --tenant <tenant uuid> --dump
```

`--connection <uuid>` is needed when a tenant has more than one active connection (two would be added into one doubled total, so the command refuses instead). `--out` names the deck, `--no-model` forces the template narrative, `--data-note` prints a line on the title slide, and `--as-of YYYY-MM-DD` treats that date as today. Without it, today is the UTC date, because the connector buckets every order by its UTC day. Exit 0 means a deck was written. Exit 2 means the report could not be produced, with the reason on stderr.

The narrative is written by a model only when `GROQ_API_KEY` or `LLM_API_KEY` is set in the environment (the provider and model are the Groq values in `app/ai/catalog_copy.py`, and the command has no flag to change them) and `--no-model` is absent. With no key the template writes it. Which writer ran, and why a model's text was refused if it was, is printed by the command, kept in the deck file's comments property and shown by `--dump`. It is not printed on any slide.

## The pieces

| File | Job |
|---|---|
| `app/report/monthly.py` | Every number: day accounting, totals, month over month change, per reported day change, formatting, and the shared wording of titles and caveats. No database, no model, no slide library. |
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

The three add up to the days in the month (or the days counted so far, for a month that is not over). Sync windows are half open, so a run ending on the 10th does not cover the 10th. Totals add the days that have orders. The days with orders plus the days with no orders are the **reported days**.

A total over fewer reported days is smaller for that reason alone, and can even fall while every reported day rose. So when a month has days with no data (or is not over, or its baseline month has days with no data) the report does not state a direction from the raw totals alone. The headline gives the total plainly and the change per reported day next to it ("October net sales total is 1.8% below September, but October is missing 2 days: per reported day it is up 1.6%"), and each total's sentence in the narrative does the same. Per reported day is the total divided by that month's reported days, computed from the exact values. A complete month keeps the plain headline ("Net sales rose 50.0% from September") with no caveat. The averages (average order value, buyers per active day) are not totals and keep the plain form with the caveat after them.

The KPI table keeps the raw totals and their change, and its Note column names the month and, for the four totals, the change per reported day ("October partial: 2 days missing; per reported day up 6.6%"). The charts show a day with no orders as a dot on the zero line on each plotted line, and a day with no data as a break in the line with an X on the zero line (grey for the previous month), and the footnote says which are present and how many of the days are plotted. A month that is not over only counts the days that are over, and says how many.

A previous month that was synced and found nothing is a **zero baseline**: the change in units is shown, the percentage is "n/a". A previous month with no rows and no run is **no previous month**, a different fact. A baseline note and the partial data note are both kept on a figure. A synced month with no orders prints its order count as 0, and its money totals as 0 when neither month has a money row, otherwise as a zero amount such as $0.00.

## The KPI table

| KPI | How it is computed |
|---|---|
| Orders, Gross sales, Refunds, Net sales | The sum of the daily values over the days that have orders. Money stays an integer in minor units and is formatted exactly (Decimal; dollars print as `$`, other currencies keep their code, zero decimal currencies such as JPY are not scaled). |
| Average order value | Gross sales divided by orders for the month, rounded half up to the minor unit like every other figure (two orders worth 19.99 together print $10.00, not $9.99). "n/a" when the month has no orders. |
| Buyers per active day | The mean of the daily distinct buyer counts, one decimal. Distinct counts cannot be summed across days, so there is no monthly total. "n/a" (never zero) when the connection withheld the metric. |
| Change | This month minus the previous month, in the metric's own unit, from the displayed figures. |
| Change % | Change divided by the absolute previous value, one decimal, rounded half up. The buyers percentage is computed from the unrounded means. "n/a" for a zero baseline. A rounded `-0.0` prints as `0.0%`. |

The words for a change: "up 8.4%" or "down 12.4%". "Unchanged" only when the two exact, unrounded values are equal. A change that merely rounds to 0.0%, or whose displayed amount is zero, is "essentially flat" and says by how much ("essentially flat (+$4.00)", or "(+0.5%)" for buyers whose means both print as 8.0).

## The narrative and the figure check

`report_facts` reduces the report to a flat set of facts using the very strings the deck prints: dollars as `$40,249.02`, dates as `Oct 14`, months by name with no year, and each total's per reported day figures. Two writers turn them into lines:

* The **template** is fixed wording filled from those strings. It is used when no model key is set, and as the fallback below.
* The **model writer** reuses the catalogue copywriter's transport and error handling (`CatalogCopyWriter._complete`, which takes a system prompt) with a report prompt. It is sent the facts, never a raw row.

Either way the text goes through `ungrounded_numbers`. It runs the catalogue check first (`verify_grounded`, a substring test that the catalogue copy keeps unchanged), then the report's own exact check:

* every amount, percentage, count and date must be one of the facts' displayed figures, written the same way, and an amount must be in the facts' currency ("EUR 210.00" on a USD store is refused; a symbol and its code are the same currency). A year or a day number is never borrowed from a date;
* a sentence that carries a figure must name a recognised metric, and the figure must be that metric's own. Metrics are named by their label or a synonym: net sales, gross sales, sales and revenue (either of the two), orders, order count, refunds, average order value, basket size, buyers, customers, shoppers. A sentence that names no metric may carry only day counts and dates, so "Net sales were $210.00. It rose 33.3% from September." is refused. A bare count beside a metric must be a day count ("2 days", "9 of 31 days");
* a comma between digits must be a valid thousands separator, so "1,4%", "6,07" and "20,15" are refused;
* a figure glued to a letter ("USD9", "x5", "Q4") is still a figure;
* number words are whole words from a fixed list and are refused unless the facts hold the same word: counts (fifteen, dozen, hundreds), multipliers (doubled, twofold, tenfold), fractions and positions (a third, a quarter, first, second), and couple, pair and several. "One" is not found inside "none".

A model's reply is also cleaned before it is checked: markdown emphasis, heading marks and list markers are stripped, and a line that ends with a colon (a preamble such as "Here is the commentary for October:") is dropped. If the reply then fails any check, has more than six lines or an exclamation mark, or has no text, or the provider errors, the whole reply is dropped and the template is used. The template goes through the same function, and the build fails if it ever quotes a figure that is not a fact. When unsure the check refuses, because the fallback is the template.

### Known limits of the figure check

Each of these is pinned by a test, so none can change silently.

* A figure of the right metric in the wrong role passes (September's net sales written as October's).
* A wrong direction word and a dropped minus sign pass ("fell" about a rise, "rose 86.4%" for a fall of 86.4%).
* The metric check is per sentence: a sentence that names two metrics may use either one's figures.
* Day counts and dates are interchangeable between the data health facts (2 days with orders can be written as 2 days with no orders; the best day's date can be written as a day with no data).
* A correct sentence written in another format is refused and the template is used ("33%" for "33.3%", "Oct 10-11" for "Oct 10, Oct 11").
* Claims and causes are not checked ("a record high", "because of the holiday sale"). The prompt forbids them and the KPI table carries the true direction.

## The deck

Seven slides: title, KPI summary table, net sales by day (this month against last month), orders by day, order value and buyers with the three best days, insights, data health. Every table cell and title is a string `monthly.py` formatted; the deck does no arithmetic. The charts are native PowerPoint charts, so the values can be read back with python-pptx. The slides use plain words (days with no data, data pull, daily figures) and no text box or table cell is smaller than 11 points, which a test enforces (chart text is set to 12 points in `deck.py`). The file's own properties are set: author, a creation time of the build, an application name and slide count in place of the template's, and no template thumbnail.

## Sample deck

Built on 2026-10-25 from invented orders (fixed seed, no store, no customer, no client), through the same code path as the command, in a scratch Postgres database that is dropped afterwards. The report date is set to 2026-11-02 (`--as-of`) so that October counts as a finished month. The store is called "Example Outfitters" on the title slide.

* [report_2026-10.pptx](monthly-report-sample/report_2026-10.pptx), the deck
* [contact-sheet.png](monthly-report-sample/contact-sheet.png) and `slide-1.png` to `slide-7.png`, rendered with LibreOffice and pdftoppm
* [report_2026-10.txt](monthly-report-sample/report_2026-10.txt), every slide read back through python-pptx, ending with the provenance line

![All seven slides of the sample deck on one sheet](monthly-report-sample/contact-sheet.png)

What the sample shows: October had 607 orders and net sales of $40,249.02 against September's $40,966.11, a total 1.8% lower. But October has 29 reported days and September 30, so per reported day net sales are up 1.6% ($1,387.90 against $1,365.54) and orders are up 6.6% (20.93 against 19.63, where the raw totals read +3.1%). 28 days have orders, 1 day has none, and 2 days (Oct 14 and Oct 15) have no data because only a failed run touched them, so every October change is marked partial. The refunds total moved most, 27.1% above September (31.5% per reported day). The sample was made with no model key, so the template wrote the insights.

Rebuild it with `python scripts/build_sample_report.py` (needs the compose Postgres, or `ANALYTICS_TEST_DSN`). If `soffice` or `pdftoppm` is not found the script says so and leaves the text read back as the proof. LibreOffice paginates and spaces text a little differently from PowerPoint, and this deck was only looked at through LibreOffice.

## Tests and guards

```bash
python -m pytest tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_cli.py tests/test_report_service_db.py -q
# the mutation harness edits source in place: run it in a COPY of the repository
python tests/mutation_check.py --only report,insights,deck
```

* `test_monthly_report.py`: the KPI table against hand computed figures (`tests/report_fixtures.py`), an empty month, a missing day, a gap in the middle, a failed run, the first month, a zero baseline, both notes kept, a month in progress, the UTC default for today, month and year boundaries including a leap February, percent rounding, the buyers percent from unrounded means, half up rounding of the average order value, flat against unchanged (buyers included), the per reported day figures for a complete month, a month where the direction does not flip and one where it does, a baseline month with a gap, and the refusals (two rows for a day, two currencies, a float, money with no currency).
* `test_report_insights.py`: the figure check from both sides (exact figures, currencies, dates, a figure beside the wrong metric, synonyms, a sentence naming no metric, commas, glued letters, number words, markdown and preambles), the known limits above pinned, the template over ten shapes of month, and the model path through a mocked transport.
* `test_report_deck.py`: the saved deck read back through python-pptx: titles, table cells, notes, chart series with gaps and markers, the dots on both lines read from the chart XML, the insights and data health slides, plain words, no ISO dates, no text under 11 points, and the file's own properties.
* `test_report_service_db.py`: against Postgres as the request role, a second tenant's rows never appear, a failed run is not coverage, and the command line writes a deck, exits 2 with the reason, and never prints a model key.
* Forty mutation guards in `tests/mutation_check.py` (keys `report`, `insights` and `deck`) break the day accounting, the zero baseline, the month boundary, the sign of the divisor in a percent change ("the percent change divides by the signed previous value"), the duplicate refusal, the previous month check, the chart gap, the headline direction, the per reported day basis, the exact figure check, the metric, synonym, currency, comma and number word checks, the model's lines, flat against unchanged, the partial note, the rounding, both date defaults, the dots on both lines, the data health list, the no data marker and the file properties, and require the report suite to go red.

The saved output of the 2026-10-25 runs (the whole suite against a scratch database, before and after, and the guard runs) is in [test-runs/2026-10-25.txt](test-runs/2026-10-25.txt).

## Limits

* One connector: a Shopify store. There is no second marketing platform, so nothing here combines ad spend or traffic with sales. A second connector is the next step named in the README.
* The sample is invented orders in a scratch database. No real store or live client went through this stage.
* The model path is tested only through a mocked transport. It was not run against a live model in this build, so how a real model behaves on the report prompt is unmeasured.
* The figure check has the known limits listed above.
* One connection and one currency per report. Money rows with two currencies in a month are refused, not converted.
* Days follow the connector's UTC bucketing.
* The deck is a fixed layout of seven slides with no template or branding option, and no delivery: nothing emails it or runs it monthly.
* The catalogue copywriter's own substring check is unchanged. Only the report uses the exact figure check.
