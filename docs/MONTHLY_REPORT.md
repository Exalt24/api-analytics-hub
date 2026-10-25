# Monthly report

The report stage reads the dated snapshots the sync already stored for one connection, works out a month's KPI table and the change on the month before in plain code, has a writer put the narrative into words from figures that passed a check, and saves a PowerPoint deck. It reads stored rows only and never calls a vendor API, the same rule the dashboard follows.

A tenant can have two sources: the store (Shopify) and a website (Google Analytics 4). With both, the deck carries the website figures next to the store's; with only the store it is the seven slide deck it always was. The website half is described under [The website source](#the-website-source), and what Google's pages said is in [GA4_SOURCES.md](GA4_SOURCES.md).

## Run it

Commands below assume the virtual environment from the README is active, so `python` is its interpreter (activate with `. .venv/Scripts/activate` in Git Bash on Windows, `. .venv/bin/activate` on macOS or Linux). Everything here was run on Windows 11 with Git Bash; the macOS and Linux forms are the standard venv layout and were not run.

```bash
export DATABASE_URL="postgresql://analytics:analytics@localhost:5433/analytics"
python scripts/monthly_report.py --month 2026-10 --tenant <tenant uuid> --dump
```

`--connection <uuid>` names the store and is needed when a tenant has more than one active store connection (two would be added into one doubled total, so the command refuses instead). `--web-connection <uuid>` does the same for a website: a tenant with one active GA4 connection gets it automatically, with two it must name one, and an id that is not an active GA4 connection of this tenant is refused. `--out` names the deck, `--no-model` forces the template narrative, `--data-note` prints a line on the title slide, and `--as-of YYYY-MM-DD` treats that date as today. Without it, today is the UTC date, because the connector buckets every order by its UTC day. Exit 0 means a deck was written. Exit 2 means the report could not be produced, with the reason on stderr.

The narrative is written by a model only when `GROQ_API_KEY` or `LLM_API_KEY` is set in the environment (the provider and model are the Groq values in `app/ai/catalog_copy.py`, and the command has no flag to change them) and `--no-model` is absent. With no key the template writes it. Which writer ran, and why a model's text was refused if it was, is printed by the command, kept in the deck file's comments property and shown by `--dump`. It is not printed on any slide.

## The pieces

| File | Job |
|---|---|
| `app/report/monthly.py` | Every number: day accounting, totals, month over month change, per reported day change, formatting, and the shared wording of titles and caveats. No database, no model, no slide library. |
| `app/report/insights.py` | The narrative: the template stand in, the model writer, and the figure check both pass through. |
| `app/report/deck.py` | The PowerPoint deck, and `dump_deck`, which reads a saved deck back as text. |
| `app/report/service.py` | The only database code: reads `metric_snapshot` and `sync_run` as the request role, so row level security applies. |
| `app/report/web.py` | The website half: the month's website figures, the order rate over the days both sources reported, the weeks, and the wording of the website headline. No database, no model, no slide library. |
| `app/connectors/ga4.py` | The GA4 connector. It is not part of the report, which only reads what it stored. |
| `app/connectors/ga4_fake.py` | The stand-in GA4 server the tests and the sample run against. |
| `scripts/sync_ga4.py` | Pulls one property's daily figures into the snapshots. Credentials from the environment only. |
| `app/report/demo_data.py` | Invented orders and invented website figures for the sample, with holes laid out on purpose. |
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

## The website source

**Which connection.** Every active connection whose platform is not `ga4` is a store, and `ga4` connections are websites. The store is chosen as before. A website is read from its own rows and its own sync runs, so the days are classified per source: the store can have a day with no data that the website has, and the other way round.

**Days.** A month of website rows goes through the same `month_stats` as the store's. A day with a row is data. A day with none that a succeeded or partial website run covers is quiet (a zero). A day with none and no covering run is missing, left out of the totals and drawn as a gap. GA4 leaves out a day whose metrics are all zero, so quiet is the normal state of a day with no traffic, and a failed run over a day is not coverage.

| Figure | How it is computed |
|---|---|
| Sessions, Page views, Key events | The sum of the daily values over the days that have data. The change on the month before, and in a partial month the change per reported day, work as for the store's totals. |
| Users per active day | The mean of the daily distinct user counts over the days that have data, one decimal, rounded half up; the percent change is from the unrounded means. Distinct counts cannot be added across days (a visitor on two days would count twice), so there is no monthly total. |
| Order rate | Orders divided by sessions, as a percent, over the days **both** sources reported (data or quiet). A day only one source covers is left out of both sides, because a sum over unequal days is not a rate. It is not a GA4 conversion rate: no GA4 event is in it, and orders the website did not send are in it. "n/a" when those days have no sessions. The change column is in points ("+0.2 pts"), the percent column is the relative change. |

A month with no website rows and no covering run has no figures, shown as "n/a" and "not reported", never as zero.

**Weeks.** The weekly table sets sessions against orders for days 1 to 7, 8 to 14 and so on, over the days both sources reported, and says how many ("5 of 7"). A week with no such day prints "n/a" in every column.

**Two calendars.** Store days are UTC days (the Shopify connector converts). The pages do not say how GA4 cuts a `date`; it is probably the property's reporting time zone, which is UNVERIFIED (GA4_SOURCES.md). The report lines the two sources up by date label and says so under the website table. It does not shift either one.

**The check on the narrative.** The website figures are facts in the same shape as the store's (`Sessions`, `Sessions change percent`, `Sessions per reported day` and so on), so the figure check holds each one beside its own metric. Sessions, users (also visitors), page views (also views), key events and order rate have the synonyms the check accepts; a sentence that names a metric the report does not have ("conversions") carries no figure it can ground and is refused. The template prints a line for sessions, key events and order rate, and the check runs over it as it does over the store's.

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

Seven slides for a store with no website: title, KPI summary table, net sales by day (this month against last month), orders by day, order value and buyers with the three best days, insights, data health.

With a website the deck is eight slides, not seven, for a reason: the website is a second source with its own days, so it needs its own table and its own data health, and the KPI slide was full. The orders chart moves beside a new sessions chart (slide 4, two charts, each against the month before), and a website slide follows the order value slide (slide 6: the website KPI table and the weekly sessions against orders table). The data health slide (slide 8) has one column per source and month and names the days with no data for each. The store only slides are unchanged, and a test compares a store only build with the saved read back of the earlier sample, character for character. Every table cell and title is a string `monthly.py` formatted; the deck does no arithmetic. The charts are native PowerPoint charts, so the values can be read back with python-pptx. The slides use plain words (days with no data, data pull, daily figures) and no text box or table cell is smaller than 11 points, which a test enforces (chart text is set to 12 points in `deck.py`). The file's own properties are set: author, a creation time of the build, an application name and slide count in place of the template's, and no template thumbnail.

## Sample deck

Built on 2026-10-25 from invented figures (fixed seed, no store, no website, no customer, no client), through the same code path as the command, in a scratch Postgres database that is dropped afterwards. The store's rows are loaded directly. The website's rows are not: the GA4 connector reads them over HTTP from the stand-in server (`app/connectors/ga4_fake.py`, answering for an invented property) and the sync runner stores them, so the website side has been through the connector, the runner and the database but **not through Google**. It has not run against a live property. The report date is set to 2026-11-02 (`--as-of`) so that October counts as a finished month. The store is called "Example Outfitters" on the title slide, and the title slide says the figures are invented and the website side ran against a stand-in.

* [report_2026-10.pptx](monthly-report-sample/report_2026-10.pptx), the deck, eight slides
* [contact-sheet.png](monthly-report-sample/contact-sheet.png) and `slide-1.png` to `slide-8.png`, rendered with LibreOffice and pdftoppm
* [report_2026-10.txt](monthly-report-sample/report_2026-10.txt), every slide read back through python-pptx, ending with the provenance line

![All eight slides of the sample deck on one sheet](monthly-report-sample/contact-sheet.png)

What the sample shows. The store half is the earlier sample unchanged: October had 607 orders and net sales of $40,249.02 against September's $40,966.11, a total 1.8% lower but up 1.6% per reported day, with 2 days (Oct 14 and Oct 15) that have no data. The website half: October had 21,812 sessions against September's 23,876, a total 8.6% lower, and 3 days (Oct 3, Oct 4 and Oct 5) with no website data because only a failed run touched them, so per reported day sessions are down 2.1% (779.00 against 795.87). Users per active day were 607.1 against 642.2. The order rate was 2.7% against 2.5% over the 26 days both sources reported. The sample was made with no model key, so the template wrote the insights.

Rebuild it with `python scripts/build_sample_report.py` (needs the compose Postgres, or `ANALYTICS_TEST_DSN`). If `soffice` or `pdftoppm` is not found the script says so and leaves the text read back as the proof. LibreOffice paginates and spaces text a little differently from PowerPoint, and this deck was only looked at through LibreOffice. The earlier store only sample's read back is kept as `tests/fixtures/store_only_report_2026-10.txt`.

## Tests and guards

```bash
python -m pytest tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_cli.py tests/test_report_service_db.py tests/test_report_web.py tests/test_report_web_deck.py tests/test_ga4_connector.py tests/test_ga4_fake_server.py tests/test_ga4_sync_db.py -q
# the mutation harness edits source in place: run it in a COPY of the repository
python tests/mutation_check.py --only report,insights,deck,ga4,web,service,m006
```

* `test_monthly_report.py`: the KPI table against hand computed figures (`tests/report_fixtures.py`), an empty month, a missing day, a gap in the middle, a failed run, the first month, a zero baseline, both notes kept, a month in progress, the UTC default for today, month and year boundaries including a leap February, percent rounding, the buyers percent from unrounded means, half up rounding of the average order value, flat against unchanged (buyers included), the per reported day figures for a complete month, a month where the direction does not flip and one where it does, a baseline month with a gap, and the refusals (two rows for a day, two currencies, a float, money with no currency).
* `test_report_insights.py`: the figure check from both sides (exact figures, currencies, dates, a figure beside the wrong metric, synonyms, a sentence naming no metric, commas, glued letters, number words, markdown and preambles), the known limits above pinned, the template over ten shapes of month, and the model path through a mocked transport.
* `test_report_deck.py`: the saved deck read back through python-pptx: titles, table cells, notes, chart series with gaps and markers, the dots on both lines read from the chart XML, the insights and data health slides, plain words, no ISO dates, no text under 11 points, and the file's own properties.
* `test_report_service_db.py`: against Postgres as the request role, a second tenant's rows never appear, a failed run is not coverage, and the command line writes a deck, exits 2 with the reason, and never prints a model key.
* `test_report_web.py`: the website figures against hand computed ones (`+36.7%` sessions, `150.0` users per day from the mean, a `4.9%` order rate over 27 joint days), a partial month, a month in progress, no earlier month, a zero baseline, a website with no rows and no sync, the weeks, and that attaching the website leaves the store's figures untouched.
* `test_report_web_deck.py`: a store only build equals the saved earlier read back, the eight slides, the website and weekly tables, both charts with their gaps, the data health table for each source, plain words and no text under 11 points, and the figure check over the website metrics from both sides (a figure beside its own metric passes, beside another metric or not computed it is refused, a model reply with a wrong website figure falls back to the template).
* `test_ga4_sync_db.py`: the stand-in server through the sync runner into Postgres and out through the report: four metrics a day stored as integers with no currency, a second sync rewriting the same rows, a restated figure replacing the stored one, a failed run recorded with its code and not counted as coverage, a stop halfway keeping the first page and its cursor, the database refusing a currency on a count and an unregistered metric, row level security between two tenants, a store only tenant getting seven slides, two websites refused unless one is named, and both commands.
* Forty mutation guards in `tests/mutation_check.py` (keys `report`, `insights` and `deck`) break the day accounting, the zero baseline, the month boundary, the sign of the divisor in a percent change ("the percent change divides by the signed previous value"), the duplicate refusal, the previous month check, the chart gap, the headline direction, the per reported day basis, the exact figure check, the metric, synonym, currency, comma and number word checks, the model's lines, flat against unchanged, the partial note, the rounding, both date defaults, the dots on both lines, the data health list, the no data marker and the file properties, and require the report suite to go red.

Forty eight more guards cover the GA4 connector (`ga4`), the website figures and weeks (`web`), the narrative and deck lines for the website (`insights`, `deck`), the report's connection choice (`service`) and the metric migration (`m006`). The connector's own tests, including a seeded property test over 40 random windows, gaps, page sizes and fault scripts, are in `test_ga4_connector.py`, and the stand-in server is itself tested in `test_ga4_fake_server.py`.

The saved output of the 2026-10-25 runs (the whole suite against a scratch database, before and after, and the guard runs) is in [test-runs/2026-10-25.txt](test-runs/2026-10-25.txt), and the GA4 pass is in [test-runs/2026-10-25-ga4.txt](test-runs/2026-10-25-ga4.txt).

## Limits

* Two sources: a Shopify store and a GA4 website. There is no ad spend, so nothing here combines advertising cost with sales. Meta is not built.
* The GA4 side has not run against a live property. The stand-in is written from Google's pages, and GA4_SOURCES.md lists what is UNVERIFIED: the Authorization header form, whether `Retry-After` is sent on a 429, the time zone a `date` is cut at, how Google restates a day after the fact, and the history it serves.
* The connector takes an access token that someone has already obtained. It does not exchange a service account key for one or refresh it.
* A day Google later reports as all zero sends no row, so the figures stored for it stay (a known limit, an expected failure in `test_ga4_sync_db.py`).
* The order rate uses the store's orders, all of them, against the website's sessions. It is a ratio of two counts, not a conversion rate, and not every order comes from a session.
* The sample is invented figures in a scratch database. No real store, property or live client went through this stage.
* The model path is tested only through a mocked transport. It was not run against a live model in this build, so how a real model behaves on the report prompt is unmeasured.
* The figure check has the known limits listed above, and the website figures inherit them.
* One store connection, one website connection and one currency per report. Money rows with two currencies in a month are refused, not converted.
* Days follow the connector's UTC bucketing for the store and, probably, the property's time zone for the website.
* The deck is a fixed layout of seven slides, or eight with a website, with no template or branding option, and no delivery: nothing emails it or runs it monthly.
* The catalogue copywriter's own substring check is unchanged. Only the report uses the exact figure check.
