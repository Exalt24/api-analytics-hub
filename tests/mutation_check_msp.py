# LIFESPAN: permanent (the repo's own mutation check for the MSP connectors and the backlog analysis, run by hand and in CI with --check-targets)
"""Mutation control for the MSP code (ConnectWise Manage, Autotask and NinjaOne connectors, their shared helpers, the records and the backlog analysis): break each guard one
at a time and prove the tests notice.

It follows tests/mutation_check_harvest_graph.py. It needs no database and no Docker, runs only the target's own test files, and demands more than "the suite went red": each
mutant names the test that must be among the failures, so a guard cannot be "covered" by an unrelated test that happens to break.

THIS EDITS THE SOURCE FILES IN PLACE. It writes each mutation into the real file, runs the tests, and writes the original bytes back. Run it in a COPY of the repository,
never in a tree a sync might commit: a killed process can leave a weakened guard in the source, and `git diff` is how to check. A normal exit, Ctrl-C and SIGTERM all
restore the files first.

    python tests/mutation_check_msp.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check_msp.py --match range,token  only the guards whose name holds a word
    python tests/mutation_check_msp.py --only connectwise   only the guards of one target (connectwise, autotask, ninjaone, common, records, analysis, ledger)
    python tests/mutation_check_msp.py                      every guard
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONN = ROOT / "app" / "connectors"
MSP = ROOT / "app" / "msp"
BASELINE_TIMEOUT_SECONDS = 300
MUTANT_TIMEOUT_SECONDS = 240

#: name -> (source file, the test files that guard it). Only files that exist are run, so the check works at every stage of the build.
TARGETS = {
    "connectwise": (CONN / "connectwise.py", ["tests/test_connectwise_connector.py"]),
    "autotask": (CONN / "autotask.py", ["tests/test_autotask_connector.py"]),
    "ninjaone": (CONN / "ninjaone.py", ["tests/test_ninjaone_connector.py"]),
    "common": (CONN / "msp_common.py", ["tests/test_connectwise_connector.py", "tests/test_autotask_connector.py", "tests/test_ninjaone_connector.py"]),
    "records": (MSP / "records.py", ["tests/test_msp_records.py", "tests/test_connectwise_connector.py", "tests/test_autotask_connector.py", "tests/test_msp_analysis.py"]),
    "ledger": (MSP / "ledger.py", ["tests/test_msp_records.py", "tests/test_msp_backlog_cli.py"]),
    "analysis": (MSP / "analysis.py", ["tests/test_msp_analysis.py", "tests/test_msp_backlog_cli.py"]),
}


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

# (target, name, [(old, new), ...], test-name fragments of which at least one must fail, why it matters)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], list[str], str]] = []


def mutations(target: str, rows: list[tuple]) -> None:
    for name, edits, expected, why in rows:
        MUTATIONS.append((target, name, edits, expected, why))


# ---------------------------------------------------------------------- ConnectWise Manage
mutations("connectwise", [
    ("company dropped from the Basic string", [('("%s+%s:%s" % (company_id.strip(), public_key, private_key))', '("%s:%s" % (public_key, private_key))')],
     ["basic_string", "refused_key"], "the company id is part of how ConnectWise reads the keys"),
    ("clientId header dropped", [('"clientId": self._client_id,', '"x-client": self._client_id,')],
     ["basic_string", "refused_key"], "the OpenAPI file marks clientId a required header"),
    ("forward-only header dropped on the first page", [("body, response = await self._get(path, params, forward_only=True)", "body, response = await self._get(path, params, forward_only=False)")],
     ["basic_string", "link_header"], "the first page must ask for the paging the later pages follow"),
    ("forward-only header dropped on the next pages", [("body, response = await self._get(nxt, None, forward_only=True)", "body, response = await self._get(nxt, None, forward_only=False)")],
     ["link_header"], "every page asks the same way, not only the first"),
    ("forward-only header never built", [('headers["pagination-type"] = "forward-only"', 'headers["x-pagination"] = "forward-only"')],
     ["basic_string", "link_header"], "paging by page number would skip records that change while it runs"),
    ("page size not asked for", [('params: dict[str, str] = {"pageSize": str(PAGE_SIZE)}', "params: dict[str, str] = {}")],
     ["basic_string"], "the page size is how a sync stays small in calls"),
    ("operator conditions dropped", [('params["conditions"] = conditions', "pass")],
     ["conditions_the_operator"], "a condition the operator gave must reach ConnectWise unchanged"),
    ("calls undercounted", [("self._spent[0] += calls", "self._spent[0] += 0")],
     ["api_calls_counts"], "the run record must count every request"),
    # ------------------------------------------------------------------ paging
    ("next host not checked", [("(parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or ", "")],
     ["another_host", "not_this_host"], "the keys must never be sent to a host the answer names"),
    ("next path prefix not checked", [('or ".." in parsed.path\n                or not parsed.path.startswith(self._base_path + "/")):', 'or ".." in parsed.path):')],
     ["not_this_host"], "the keys must only go to the API path"),
    ("next dotdot not checked", [(' or ".." in parsed.path\n                or not parsed.path.startswith(self._base_path + "/")):', '\n                or not parsed.path.startswith(self._base_path + "/")):')],
     ["not_this_host"], "a path that climbs out of the API path is another endpoint"),
    ("loop not noticed", [('if nxt in seen:\n                raise UpstreamBroken("a next page names itself")', 'if False:\n                raise UpstreamBroken("a next page names itself")')],
     ["empty_page_that_links"], "a next page that names itself would loop"),
    ("duplicate record allowed", [('if r["id"] in ids:\n                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], path))', 'if False:\n                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], path))')],
     ["malformed_ticket", "changed_cursor"], "the same ticket twice doubles a day"),
    ("list shape not checked", [("if not isinstance(body, list):", "if False:")],
     ["list_that_is_an_object"], "an object where a list belongs is a changed answer, not no tickets"),
    ("cut body not refused", [('except ValueError:\n            raise UpstreamBroken("ConnectWise answered 200 with a body that is not JSON") from None', 'except ZeroDivisionError:\n            raise UpstreamBroken("ConnectWise answered 200 with a body that is not JSON") from None')],
     ["body_cut_short"], "a page cut short must not chart as fewer tickets"),
    ("record id type not checked", [("return isinstance(value, int) and not isinstance(value, bool)", "return isinstance(value, int)")],
     ["malformed_ticket"], "true is not a ticket number"),
    # ------------------------------------------------------------------ the ticket
    ("dateEntered not required", [('if not isinstance(info, dict) or "dateEntered" not in info:', "if False:")],
     ["malformed_ticket"], "a ticket with no creation date cannot be placed on a day"),
    ("closedFlag type not checked", [('if not isinstance(r.get("closedFlag"), bool):', "if False:")],
     ["malformed_ticket"], "a closedFlag that is text would count every ticket as closed"),
    ("closed without a closedDate allowed", [('if r.get("closedDate") is None:', "if False:")],
     ["malformed_ticket"], "a closed ticket with no date would drop out of the closed count"),
    ("summary type not checked", [("if not isinstance(summary, str):", "if False:")],
     ["malformed_ticket"], "a summary that is not text is a changed answer"),
    ("empty summary label changed", [("title=summary.strip() or NO_SUMMARY", 'title=summary.strip() or "x"')],
     ["empty_summary"], "a blank summary is shown as such, not as a made up title"),
    ("board not required", [('"the board of ticket %d" % tid, required=True)', '"the board of ticket %d" % tid, required=False)')],
     ["malformed_ticket"], "the board is the category the analysis groups by"),
    ("status not required", [('"the status of ticket %d" % tid, required=True)', '"the status of ticket %d" % tid, required=False)')],
     ["malformed_ticket"], "a ticket with no status cannot be called open or not"),
    ("member not required", [('if who is None:\n            raise UpstreamBroken("time entry %d names no member" % eid)', 'if False:\n            raise UpstreamBroken("time entry %d names no member" % eid)')],
     ["identifier_is_named"], "time with no technician cannot be spread per technician"),
    ("time entry ticket link dropped", [('ticket_id = str(ticket["id"]) if isinstance(ticket, dict) and _is_id(ticket.get("id")) else None', "ticket_id = None")],
     ["time_entries_become_records"], "time that is not tied to its ticket cannot be spread per category"),
    # ------------------------------------------------------------------ the figures
    ("goal met exactly counted as missed", [("(done is not None and done > due)", "(done is not None and done >= due)")],
     ["goal_met_to_the_second", "seeded_worlds"], "finishing on the goal is meeting it"),
    ("open at the goal counted as missed", [("(done is None and as_of > due)", "(done is None and as_of >= due)")],
     ["goal_met_to_the_second", "seeded_worlds"], "a goal that is this very second is not yet missed"),
    ("closed time used instead of resolved", [("done = t.finished_at", "done = t.closed_at")],
     ["resolved_on_time_but_closed_late", "seeded_worlds"], "the goal is answered when the work is resolved, not when the ticket is tidied away"),
    ("time seconds undercounted", [("seconds[utc_day(e.worked_on)] = seconds.get(utc_day(e.worked_on), 0) + e.seconds", "seconds[utc_day(e.worked_on)] = seconds.get(utc_day(e.worked_on), 0) + 1")],
     ["four_counts", "seeded_worlds"], "time is summed in whole seconds"),
    ("closed counted on the opened day", [("closed[utc_day(t.closed_at)] = closed.get(utc_day(t.closed_at), 0) + 1", "closed[utc_day(t.opened_at)] = closed.get(utc_day(t.opened_at), 0) + 1")],
     ["four_counts", "seeded_worlds"], "a ticket closed days later belongs to the day it closed"),
    ("missed counted on the opened day", [("missed[utc_day(due)] = missed.get(utc_day(due), 0) + 1", "missed[utc_day(t.opened_at)] = missed.get(utc_day(t.opened_at), 0) + 1")],
     ["four_counts", "seeded_worlds"], "a goal missed belongs to the day the goal fell on"),
    # ------------------------------------------------------------------ refresh
    ("refresh reads closed tickets too", [("                if t.is_open:\n                    body, _ =", "                if True:\n                    body, _ =")],
     ["refresh_open_sla_reads"], "only open tickets have an SLA that is still moving"),
    ("refresh answer for another ticket accepted", [('body.get("id") != int(t.ticket_id)', "False")],
     ["refresh_answer_for_another"], "an answer for a different ticket must not replace this one"),
    ("refreshed ticket thrown away", [("out[i] = self._ticket(body)", "pass")],
     ["refresh_open_sla_reads", "refresh_changes"], "the refresh is only worth its calls if its answer is used"),
    # ------------------------------------------------------------------ errors and inputs
    ("401 not revoked", [('if http == 401:\n            raise AuthRevoked(', 'if False:\n            raise AuthRevoked(')],
     ["refused_key", "wrong_private"], "a 401 is a person fixing the keys, not a retry"),
    ("429 not retried", [('if http == 429:\n            return Retry(retry_after(response), "HTTP 429")', 'if False:\n            return Retry(retry_after(response), "HTTP 429")')],
     ["short_retry_after", "longer_than_the_cap", "no_retry_after"], "a 429 says wait, not stop"),
    ("retry-after ignored", [('return Retry(retry_after(response), "HTTP 429")', 'return Retry(None, "HTTP 429")')],
     ["short_retry_after", "longer_than_the_cap"], "the vendor's own wait is used when it gives one"),
    ("5xx not retried", [("if http in (500, 502, 503, 504):", "if False:")],
     ["server_error"], "a 503 is the vendor saying try again"),
    ("cursor ignored", [('if cursor:\n            raise ValueError("the ConnectWise connector', 'if False:\n            raise ValueError("the ConnectWise connector')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    ("empty window allowed", [('if window_end <= window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:',
                               'if window_end < window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:')],
     ["cursor_and_an_empty_window"], "a window of no days is a caller bug"),
    ("naive clock allowed", [("if as_of.tzinfo is None:", "if False:")],
     ["clock_with_no_zone"], "a clock with no zone makes every open goal a guess"),
    ("credential presence not checked", [('if not isinstance(value, str) or not value.strip():\n                raise ValueError("%s is required" % name)', 'if False:\n                raise ValueError("%s is required" % name)')],
     ["bad_options"], "an empty key is refused before a request, not by a 401"),
    ("company id separators not checked", [('if "+" in company_id or ":" in company_id:', "if False:")],
     ["bad_options"], "a + or : in the company id would change what the Basic string means"),
    ("public key colon not checked", [('if ":" in public_key:', "if False:")],
     ["bad_options"], "a : in the public key would change what the Basic string means"),
    ("api path not required", [("if not urlparse(self._base).path:", "if False:")],
     ["api_address_that_is_not"], "the host alone is not the API"),
])

# ---------------------------------------------------------------------- Autotask
mutations("autotask", [
    ("zone lookup sends the credentials", [("            if authenticated:\n", "            if True:\n")],
     ["zone_is_asked_first"], "the zone call needs no authentication, so none is sent to a lookup address"),
    ("user name header dropped", [('"Username": self.username,', '"X-User": self.username,')],
     ["zone_is_asked_first", "refused_credential"], "the security page names Username as a required header"),
    ("secret header dropped", [('"Secret": self._secret.reveal(),', '"X-Secret": self._secret.reveal(),')],
     ["zone_is_asked_first", "refused_credential"], "the security page names Secret as a required header"),
    ("integration code header dropped", [('"APIIntegrationcode": self._code.reveal()', '"X-Code": self._code.reveal()')],
     ["zone_is_asked_first", "refused_credential"], "the REST API requires a tracking identifier for API-only users"),
    ("content type dropped", [('headers = {"Accept": "application/json", "Content-Type": "application/json"}', 'headers = {"Accept": "application/json"}')],
     ["zone_is_asked_first"], "the security page's headers include the content type"),
    ("zone host not checked", [('(parsed.scheme == "https" and (parsed.hostname or "").endswith(".autotask.net"))', '(parsed.scheme == "https")')],
     ["zone_answer_that_is_not_autotask"], "the credentials must only go to an Autotask host"),
    ("zone host suffix loosened", [('.endswith(".autotask.net")', '.endswith("autotask.net")')],
     ["zone_answer_that_is_not_autotask"], "evilautotask.net is not an autotask.net host"),
    ("loopback zone accepted from the real lookup", [("(lookup_loopback and parsed.hostname in LOOPBACK and parsed.scheme == \"http\")", "(parsed.hostname in LOOPBACK and parsed.scheme == \"http\")")],
     ["loopback_zone_is_refused"], "a real lookup must never send the credentials to a local address"),
    ("zone path not checked", [('or parsed.path.rstrip("/") != "/atservicesrest":', ":")],
     ["zone_answer_that_is_not_autotask"], "the zone's API is at /atservicesrest"),
    ("zone user info and query allowed", [("or parsed.username or parsed.password or parsed.query or parsed.fragment ", "")],
     ["zone_answer_that_is_not_autotask"], "a user name in the zone address would be sent as a credential"),
    ("zone url missing allowed", [("if not isinstance(url, str):\n            raise UpstreamBroken(\"the zone answer names no url\")", "if False:\n            raise UpstreamBroken(\"the zone answer names no url\")")],
     ["zone_with_no_url"], "a zone answer with no address is a changed answer"),
    ("api address path not required", [('if self._api_root is not None and urlparse(self._api_root).path != "/atservicesrest":', "if False:")],
     ["address_that_is_not_https"], "the host alone is not the REST API"),
    # ------------------------------------------------------------------ paging
    ("next host not checked", [("(parsed.scheme, parsed.netloc) != (root.scheme, root.netloc) or ", "")],
     ["next_page_that_is_not_this_host"], "the credentials must never be sent to a host the answer names"),
    ("next path not checked", [('or ".." in parsed.path or not parsed.path.startswith(API_PATH + "/")):', "):")],
     ["next_page_that_is_not_this_host"], "the credentials must only go to the REST API path"),
    ("next dotdot not checked", [('or ".." in parsed.path or not parsed.path.startswith(API_PATH + "/")):', 'or not parsed.path.startswith(API_PATH + "/")):')],
     ["next_page_that_is_not_this_host"], "a path that climbs out of the API path is another endpoint"),
    ("next url not checked at all", [("nxt = self._checked_next(nxt)", "nxt = nxt")],
     ["next_page_that_is_not_this_host"], "every next page is checked before it is called"),
    ("loop not noticed", [('if nxt in seen:\n                raise UpstreamBroken("a next page names itself")', 'if False:\n                raise UpstreamBroken("a next page names itself")')],
     ["names_itself"], "a next page that names itself would loop"),
    ("duplicate record allowed", [('if r["id"] in ids:\n                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], entity))', 'if False:\n                    raise UpstreamBroken("record %d of %s appears twice" % (r["id"], entity))')],
     ["malformed_ticket"], "the same ticket twice doubles a day"),
    ("count not checked", [("if _is_id(count) and count != len(items):", "if False:")],
     ["count_that_does_not_match"], "a page that arrived short must not chart as fewer tickets"),
    ("page size not checked", [("if len(items) > MAX_RECORDS:", "if False:")],
     ["page_larger_than"], "a page over the size asked for is a changed answer"),
    ("answer shape not checked", [("if not isinstance(items, list) or not isinstance(details, dict):", "if False:")],
     ["without_items_and_page_details"], "an answer with no items list is not no tickets"),
    ("cut body not refused", [('except ValueError:\n            raise UpstreamBroken("Autotask answered 200 with a body that is not JSON") from None', 'except ZeroDivisionError:\n            raise UpstreamBroken("Autotask answered 200 with a body that is not JSON") from None')],
     ["body_that_is_not_json"], "a body cut short must not chart as fewer tickets"),
    ("record id type not checked", [("return isinstance(value, int) and not isinstance(value, bool)", "return isinstance(value, int)")],
     ["malformed_ticket"], "true is not a ticket number"),
    # ------------------------------------------------------------------ the request
    ("page size not asked for in the ticket query", [('search = {"MaxRecords": MAX_RECORDS, "IncludeFields": TICKET_FIELDS,\n', 'search = {"IncludeFields": TICKET_FIELDS,\n')],
     ["names_the_window"], "the page size is how a sync stays small in calls"),
    ("id left out of the fields", [('TICKET_FIELDS = ["id", "title",', 'TICKET_FIELDS = ["title",')],
     ["names_the_window", "connector_never_does"], "a query over one page must name Id as a field"),
    ("rich text fields asked for", [('"status", "priority", "ticketCategory", "issueType", "companyID"]\nTIME_FIELDS', '"status", "priority", "ticketCategory", "issueType", "companyID", "description"]\nTIME_FIELDS')],
     ["names_the_window"], "the rich text fields are never needed"),
    ("goal day branch dropped", [("window(self._resolution_field), ", "")],
     ["names_the_window", "completed_after_the_window", "seeded_worlds"], "a goal missed on a day inside the window is counted whenever the ticket was completed"),
    ("open branch dropped", [(', {"op": "notExist", "field": "completedDate"}', "")],
     ["names_the_window", "seeded_worlds"], "the open backlog is read whole"),
    ("window end inclusive", [('{"op": "lt", "field": field, "value": _stamp(end)}', '{"op": "lte", "field": field, "value": _stamp(end)}')],
     ["names_the_window"], "the window is half open"),
    ("window start exclusive", [('{"op": "gte", "field": field, "value": _stamp(start)}', '{"op": "gt", "field": field, "value": _stamp(start)}')],
     ["names_the_window"], "the first day of the window is in it"),
    ("time window start exclusive", [('{"op": "gte", "field": "dateWorked", "value": _stamp(start)}', '{"op": "gt", "field": "dateWorked", "value": _stamp(start)}')],
     ["names_the_window"], "the first day of the window is in it"),
    ("id chunk too large", [("ID_CHUNK = 100", "ID_CHUNK = 1000")],
     ["older_completed_ticket"], "a list of ids is cut into small queries"),
    ("known tickets looked up again", [("- known)", ")")],
     ["api_calls_counts"], "a ticket the query returned is not asked for again"),
    ("older tickets not kept as records", [("self.tickets_read, self.time_read = primary + extra, times", "self.tickets_read, self.time_read = primary, times")],
     ["older_completed_ticket"], "time on an older ticket needs its category"),
    # ------------------------------------------------------------------ pacing
    ("threshold not checked before the queries", [("        await self._check_headroom()\n        labels = await self._labels()\n        primary", "        labels = await self._labels()\n        primary")],
     ["nearly_spent", "read_from_the_platform"], "a sync must stop before it spends the hour's requests"),
    ("headroom edge inclusive", [("if limit - used < MIN_CALLS_LEFT:", "if limit - used <= MIN_CALLS_LEFT:")],
     ["nearly_spent"], "exactly the margin left is enough"),
    ("headroom never refused", [("if limit - used < MIN_CALLS_LEFT:", "if False:")],
     ["nearly_spent", "read_from_the_platform"], "the platform's own count is the pace"),
    ("threshold answer not checked", [("if not _is_id(limit) or not _is_id(used) or limit < 1 or used < 0:", "if False:")],
     ["not_whole_numbers"], "a count that is not a number must not read as headroom"),
    # ------------------------------------------------------------------ picklists and the ticket
    ("picklist list not checked", [("if not isinstance(fields, list):", "if False:")],
     ["picklist_answer_that_is_malformed"], "no field list is a changed answer"),
    ("picklist values not checked", [("if not isinstance(values, list):", "if False:")],
     ["picklist_answer_that_is_malformed"], "a picklist with no values is a changed answer"),
    ("picklist value shape not checked", [('if not isinstance(v, dict) or not isinstance(v.get("value"), str) or not isinstance(v.get("label"), str):', "if False:")],
     ["picklist_answer_that_is_malformed"], "a value with no label is a changed answer"),
    ("picklist label not trimmed", [('= v["label"].strip()', '= v["label"]')],
     ["trimmed_and_an_empty"], "a label is shown as a person reads it"),
    ("empty label shown as empty", [('return label if label else "%s %d" % (field, value)', "return label")],
     ["trimmed_and_an_empty"], "a value with no label is named by its number"),
    ("picklist number not checked", [('if not _is_id(value):\n            raise UpstreamBroken("the %s of a ticket is not a picklist number" % field)', 'if False:\n            raise UpstreamBroken("the %s of a ticket is not a picklist number" % field)')],
     ["malformed_ticket"], "a status that is text is a changed answer"),
    ("status not required", [("if status is None:", "if False:")],
     ["malformed_ticket"], "a ticket with no status cannot be called open or not"),
    ("company id type not checked", [("if company is not None and not _is_id(company):", "if False:")],
     ["malformed_ticket"], "a company that is not a number is a changed answer"),
    ("title type not checked", [("if not isinstance(title, str):", "if False:")],
     ["malformed_ticket"], "a title that is not text is a changed answer"),
    ("blank title label changed", [('title=title.strip() or "(no title)"', 'title=title.strip() or "x"')],
     ["blank_title"], "a blank title is shown as such, not as a made up one"),
    ("createDate not required", [("            if r.get(key) is None:\n                if required:", "            if r.get(key) is None:\n                if False:")],
     ["malformed_ticket"], "a ticket with no creation date cannot be placed on a day"),
    ("completion does not close", [("closed=completed is not None,", "closed=False,")],
     ["four_counts", "completed_ticket_is_closed"], "a completed ticket is closed whatever its status number"),
    ("ticket time zone assumption dropped", [('return parse_instant(r[key], "the %s of ticket %d" % (key, tid), naive_is_utc=True)', 'return parse_instant(r[key], "the %s of ticket %d" % (key, tid), naive_is_utc=False)')],
     ["four_counts", "dates_with_no_zone"], "the page says every time is UTC"),
    ("time time zone assumption dropped", [('"the dateWorked of time entry %d" % eid, naive_is_utc=True)', '"the dateWorked of time entry %d" % eid, naive_is_utc=False)')],
     ["four_counts"], "the page says every time is UTC"),
    ("hours field misread", [('r.get("hoursWorked"), "the hoursWorked of time entry %d" % eid)', 'r.get("hoursToBill"), "the hoursWorked of time entry %d" % eid)')],
     ["four_counts", "seeded_worlds"], "hours worked, not hours to bill"),
    ("resource not required", [("if not _is_id(resource):", "if False:")],
     ["malformed_time_entry"], "time with no technician cannot be spread per technician"),
    ("time ticket id type not checked", [("if ticket is not None and not _is_id(ticket):", "if False:")],
     ["malformed_time_entry"], "a ticketID that is text is a changed answer"),
    ("time ticket link dropped", [('ticket_id=None if ticket is None else str(ticket)', "ticket_id=None")],
     ["older_completed_ticket"], "time that is not tied to its ticket cannot be spread per category"),
    # ------------------------------------------------------------------ the figures
    ("goal met exactly counted as missed", [("(done is not None and done > due)", "(done is not None and done >= due)")],
     ["goal_met_to_the_second", "seeded_worlds"], "finishing on the goal is meeting it"),
    ("open at the goal counted as missed", [("(done is None and as_of > due)", "(done is None and as_of >= due)")],
     ["goal_met_to_the_second", "seeded_worlds"], "a goal that is this very second is not yet missed"),
    ("completed time used instead of resolved", [("done = t.finished_at", "done = t.closed_at")],
     ["resolved_on_time_but_completed_late", "seeded_worlds"], "the goal is answered when the work is resolved, not when the ticket is completed"),
    ("time seconds undercounted", [("seconds[utc_day(e.worked_on)] = seconds.get(utc_day(e.worked_on), 0) + e.seconds", "seconds[utc_day(e.worked_on)] = seconds.get(utc_day(e.worked_on), 0) + 1")],
     ["four_counts", "seeded_worlds"], "time is summed in whole seconds"),
    ("completed counted on the opened day", [("closed[utc_day(t.closed_at)] = closed.get(utc_day(t.closed_at), 0) + 1", "closed[utc_day(t.opened_at)] = closed.get(utc_day(t.opened_at), 0) + 1")],
     ["four_counts", "seeded_worlds"], "a ticket completed days later belongs to the day it was completed"),
    ("missed counted on the opened day", [("missed[utc_day(due)] = missed.get(utc_day(due), 0) + 1", "missed[utc_day(t.opened_at)] = missed.get(utc_day(t.opened_at), 0) + 1")],
     ["four_counts", "seeded_worlds"], "a goal missed belongs to the day the goal fell on"),
    # ------------------------------------------------------------------ errors and inputs
    ("401 not revoked", [('if http == 401:\n            raise AuthRevoked(', 'if False:\n            raise AuthRevoked(')],
     ["refused_credential"], "a 401 is a person fixing the credentials, not a retry"),
    ("429 not retried", [('if http == 429:\n            return Retry(retry_after(response), "HTTP 429")', 'if False:\n            return Retry(retry_after(response), "HTTP 429")')],
     ["no_retry_after", "longer_than_the_cap"], "a 429 says wait, not stop"),
    ("retry-after ignored", [('return Retry(retry_after(response), "HTTP 429")', 'return Retry(None, "HTTP 429")')],
     ["no_retry_after", "longer_than_the_cap"], "the vendor's own wait is used when it gives one"),
    ("5xx not retried", [("if http in (500, 502, 503, 504):", "if False:")],
     ["server_error"], "a 503 is the vendor saying try again"),
    ("cursor ignored", [('if cursor:\n            raise ValueError("the Autotask connector', 'if False:\n            raise ValueError("the Autotask connector')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    ("empty window allowed", [('if window_end <= window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:',
                               'if window_end < window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:')],
     ["cursor_and_an_empty_window"], "a window of no days is a caller bug"),
    ("naive clock allowed", [("if as_of.tzinfo is None:", "if False:")],
     ["clock_with_no_zone"], "a clock with no zone makes every open goal a guess"),
    ("credential presence not checked", [('if not isinstance(value, str) or not value.strip():\n                raise ValueError("%s is required" % name)', 'if False:\n                raise ValueError("%s is required" % name)')],
     ["bad_options"], "an empty credential is refused before a request, not by a 401"),
    ("user name shape not checked", [('if "@" not in username:', "if False:")],
     ["bad_options"], "the zone lookup is by the API user's email address"),
    ("resolution field not checked", [('if resolution_due_field not in ("resolvedDueDateTime", "dueDateTime"):', "if False:")],
     ["resolution_field_option"], "only the two fields the entity page lists as due dates are offered"),
    ("verify answer not checked", [('if not isinstance(info, dict) or not isinstance(info.get("name"), str):', "if False:")],
     ["not_an_entity_description"], "a connect that does not read an entity description proves nothing"),
])

# ---------------------------------------------------------------------- NinjaOne
mutations("ninjaone", [
    ("grant type changed", [('"grant_type": "client_credentials",', '"grant_type": "password",')],
     ["token_request_is_client_credentials"], "the flow page names client_credentials for an app that signs in as itself"),
    ("scope not asked for", [('"client_secret": self._secret.reveal(), "scope": SCOPE},', '"client_secret": self._secret.reveal()},')],
     ["token_request_is_client_credentials"], "the sync asks for the least scope it needs"),
    ("scope widened", [('SCOPE = "monitoring"', 'SCOPE = "management"')],
     ["token_request_is_client_credentials", "without_the_monitoring_scope"], "monitoring is read only, management is not"),
    ("secret not sent", [('"client_secret": self._secret.reveal(), "scope": SCOPE', '"client_secret": "x", "scope": SCOPE')],
     ["token_request_is_client_credentials", "refused_client"], "the secret is how NinjaOne knows the app"),
    ("invalid scope not read as a refusal", [('"unauthorized_client", "invalid_scope", ', '"unauthorized_client", ')],
     ["without_the_monitoring_scope"], "an app without the scope needs a person, not a retry"),
    ("oauth error word ignored", [("        if _error_word(response):\n", "        if False:\n")],
     ["refused_client", "without_the_monitoring_scope"], "a refused client is a person fixing the app registration"),
    ("token shape not checked", [('if not isinstance(token, str) or not token or not isinstance(body.get("token_type"), str) or body["token_type"].lower() != "bearer":', "if False:")],
     ["not_a_bearer_token"], "a token answer with no bearer token is a changed answer"),
    ("bearer word case sensitive", [('body["token_type"].lower() != "bearer"', 'body["token_type"] != "bearer"')],
     ["not_a_bearer_token", "four_gauges", "gauges_are_written"], "Bearer and bearer are the same word"),
    ("token lifetime not checked", [("if isinstance(expires, bool) or not isinstance(expires, (int, Decimal)) or expires <= 0:", "if False:")],
     ["not_a_bearer_token"], "a token with no lifetime cannot be renewed in time"),
    ("renewal point late", [("self._mono() >= self._expires - RENEW_MARGIN", "self._mono() >= self._expires")],
     ["reused_until_a_minute"], "the token is renewed a minute before it expires"),
    ("renewal point exclusive", [("self._mono() >= self._expires - RENEW_MARGIN", "self._mono() > self._expires - RENEW_MARGIN")],
     ["reused_until_a_minute"], "at the renewal point the token is renewed"),
    ("renewal margin removed", [("RENEW_MARGIN = 60.0", "RENEW_MARGIN = 0.0")],
     ["reused_until_a_minute"], "a request must not carry a token that expires on the way"),
    ("token mint not locked", [("        async with self._lock:\n            if self._token is None or", "        if True:\n            if self._token is None or")],
     ["start_together_renew"], "six requests starting together must not mint six tokens"),
    ("renewal after a 401 not shared", [("            if self._token is not None and self._token.reveal() != rejected:\n                return self._token.reveal()                          # another request already renewed it\n", "")],
     ["all_get_a_401_together"], "the first request refused renews, the others use that token"),
    ("401 not noticed", [("            if response.status_code != 401:\n                break", "            if True:\n                break")],
     ["a_401_on_a_call"], "a 401 is the token ending, not an answer"),
    ("second 401 not an error", [("            if attempt == 1:\n                raise AuthRevoked(", "            if False:\n                raise AuthRevoked(")],
     ["does_not_fix_is_auth_revoked"], "a new token that is also refused is a person's problem"),
    ("bearer word dropped", [('"Authorization": "Bearer " + bearer', '"Authorization": bearer')],
     ["token_request_is_client_credentials", "the_requests_are_devices"], "the token goes in as a bearer token"),
    # ------------------------------------------------------------------ paging
    ("page size not asked for", [('params = {"pageSize": str(self._page_size)}', "params = {}")],
     ["the_requests_are_devices"], "the page size is how a sync stays small in calls"),
    ("after not sent", [('params["after"] = str(after)', "pass")],
     ["after_until_an_empty_page", "the_requests_are_devices", "cursor_does_not_rise"], "the next page is asked for by the last id"),
    ("rising ids not checked", [('if after is not None and r["id"] <= after:', "if False:")],
     ["cursor_does_not_rise", "out_of_order"], "a cursor that does not rise would loop"),
    ("device list shape not checked", [('if not isinstance(body, list):\n                raise UpstreamBroken("the devices answer is not a list")', 'if False:\n                raise UpstreamBroken("the devices answer is not a list")')],
     ["list_that_is_an_object"], "an object where a list belongs is a changed answer"),
    ("alert list shape not checked", [('if not isinstance(body, list):\n            raise UpstreamBroken("the alerts answer is not a list")', 'if False:\n            raise UpstreamBroken("the alerts answer is not a list")')],
     ["list_that_is_an_object"], "an object where a list belongs is a changed answer"),
    ("device id type not checked", [("                if not isinstance(r, dict) or not _is_id(r.get(\"id\")):", "                if not isinstance(r, dict):")],
     ["malformed_device"], "true is not a device number"),
    ("record id type not checked", [("return isinstance(value, int) and not isinstance(value, bool)", "return isinstance(value, int)")],
     ["malformed_device", "malformed_alert"], "true is not a device number"),
    ("cut body not refused", [('except ValueError:\n            raise UpstreamBroken("NinjaOne answered 200 with a body that is not JSON") from None', 'except ZeroDivisionError:\n            raise UpstreamBroken("NinjaOne answered 200 with a body that is not JSON") from None')],
     ["body_that_is_not_json"], "a body cut short must not chart as fewer devices"),
    ("verify answer not checked", [('if not isinstance(body, list):\n            raise UpstreamBroken("the organizations answer is not a list")', 'if False:\n            raise UpstreamBroken("the organizations answer is not a list")')],
     ["verify_that_is_not_a_list"], "a connect that reads nothing proves nothing"),
    # ------------------------------------------------------------------ the records
    ("offline type not checked", [('if not isinstance(r.get("offline"), bool):', "if False:")],
     ["malformed_device"], "an offline flag that is text would count every device as offline"),
    ("organization type not checked", [("if org is not None and not _is_id(org):", "if False:")],
     ["malformed_device"], "an organization that is not a number is a changed answer"),
    ("device name fallback changed", [('or "device %d" % did', 'or "x"')],
     ["name_falls_back"], "a device with no name is named by its id"),
    ("last contact always read", [("last_contact=None if contact is None else self._instant(contact, \"the lastContact of device %d\" % did))", "last_contact=self._instant(contact, \"the lastContact of device %d\" % did))")],
     ["gauges_are_written", "one_day_window"], "a device that never reported has no last contact"),
    ("unit not applied", [('seconds = number / 1000 if self._unit == "milliseconds" else number', "seconds = number")],
     ["timestamp_unit_option"], "milliseconds are not seconds"),
    ("timestamp sanity bound removed", [("if not _MIN_YEAR <= moment.year <= _MAX_YEAR:", "if False:")],
     ["malformed_alert", "does_not_match_the_answer"], "a time in 1970 or 2200 is the wrong unit, not a time"),
    ("timestamp overflow not refused", [("except (OverflowError, OSError, ValueError):", "except ZeroDivisionError:")],
     ["malformed_device"], "a number too large to be a time must not crash the sync with a type error"),
    ("alert uid not required", [('if not isinstance(uid, str) or not uid.strip():', "if False:")],
     ["malformed_alert"], "an alert with no id cannot be told from another"),
    ("alert device not required", [('if not _is_id(r.get("deviceId")):', "if False:")],
     ["malformed_alert"], "an alert on no device cannot be spread per device"),
    ("alert source type not required", [("if source_type is None:", "if False:")],
     ["malformed_alert"], "the source type is what the alert drivers group by"),
    ("alert not an object accepted", [('if not isinstance(r, dict):\n                raise UpstreamBroken("an alert is %s, not an object" % type(r).__name__)', 'if False:\n                raise UpstreamBroken("an alert is %s, not an object" % type(r).__name__)')],
     ["malformed_alert"], "a number in the alert list is a changed answer"),
    ("duplicate alert allowed", [('if len(set(ids)) != len(ids):\n            raise UpstreamBroken("an alert appears twice")', 'if False:\n            raise UpstreamBroken("an alert appears twice")')],
     ["malformed_alert"], "the same alert twice doubles a count"),
    ("device name from the alert dropped", [('name = self._text(device.get("displayName")) or self._text(device.get("systemName"))', "name = None")],
     ["alert_becomes_a_record"], "an alert names its device"),
    ("organization from the alert dropped", [("organization_id=org if _is_id(org) else None,", "organization_id=None,")],
     ["alert_becomes_a_record"], "an alert belongs to an organization"),
    ("severity dropped", [('severity=self._text(r.get("severity")),', "severity=None,")],
     ["alert_becomes_a_record", "kept_as_the_text"], "the severity NinjaOne sends is kept"),
    ("text not trimmed", [("return value.strip() if isinstance(value, str) and value.strip() else None", "return value if isinstance(value, str) and value.strip() else None")],
     ["kept_as_the_text", "a_device_becomes_a_record"], "a name is shown as a person reads it"),
    # ------------------------------------------------------------------ the figures
    ("gauges written on the first day", [("last = window_end.fromordinal(window_end.toordinal() - 1)", "last = window_start")],
     ["gauges_are_written", "one_day_window", "seeded_worlds"], "the state when the sync ran belongs to the window's last day"),
    ("offline counted the wrong way", [("sum(1 for d in devices if d.offline)", "sum(1 for d in devices if not d.offline)")],
     ["gauges_are_written", "seeded_worlds"], "offline is offline"),
    ("devices undercounted", [('CanonicalPoint("nj_devices", last, len(devices), None, raw)', 'CanonicalPoint("nj_devices", last, len(devices) - 1, None, raw)')],
     ["gauges_are_written", "seeded_worlds"], "every device is counted"),
    ("alerts created miscounted", [("created[utc_day(a.created_at)] = created.get(utc_day(a.created_at), 0) + 1", "created[utc_day(a.created_at)] = created.get(utc_day(a.created_at), 0) + 2")],
     ["gauges_are_written", "seeded_worlds"], "each alert is one"),
    # ------------------------------------------------------------------ errors and inputs
    ("429 not retried", [('if http == 429:\n            return Retry(retry_after(response), "HTTP 429")\n        if http in (500, 502, 503, 504):\n            return Retry(retry_after(response), "HTTP %d" % http)\n        raise UpstreamBroken("NinjaOne refused the request', 'if False:\n            return Retry(retry_after(response), "HTTP 429")\n        if http in (500, 502, 503, 504):\n            return Retry(retry_after(response), "HTTP %d" % http)\n        raise UpstreamBroken("NinjaOne refused the request')],
     ["429_is_retried", "longer_than_the_cap"], "a 429 says wait, not stop"),
    ("5xx not retried", [('if http in (500, 502, 503, 504):\n            return Retry(retry_after(response), "HTTP %d" % http)\n        raise UpstreamBroken("NinjaOne refused the request', 'if False:\n            return Retry(retry_after(response), "HTTP %d" % http)\n        raise UpstreamBroken("NinjaOne refused the request')],
     ["server_error"], "a 503 is the vendor saying try again"),
    ("cursor ignored", [('if cursor:\n            raise ValueError("the NinjaOne connector', 'if False:\n            raise ValueError("the NinjaOne connector')],
     ["cursor_and_an_empty_window"], "a cursor this connector never issued must not be silently dropped"),
    ("empty window allowed", [('if window_end <= window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:',
                               'if window_end < window_start:\n            raise ValueError("the window must hold at least one day")\n        if cursor:')],
     ["cursor_and_an_empty_window"], "a window of no days is a caller bug"),
    ("credential presence not checked", [('if not isinstance(value, str) or not value.strip():\n                raise ValueError("%s is required" % name)', 'if False:\n                raise ValueError("%s is required" % name)')],
     ["bad_options"], "an empty credential is refused before a request, not by the vendor"),
    ("timestamp unit not required", [('if epoch_unit not in ("seconds", "milliseconds"):', "if False:")],
     ["bad_options"], "the unit is never guessed"),
    ("page size not checked", [("if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:", "if False:")],
     ["bad_options"], "a page size of 0 or 5000 is a typo"),
])

# ---------------------------------------------------------------------- the backlog analysis
mutations("analysis", [
    ("each digit its own mark", [('re.sub(r"\\d+", "#", title.lower())', 're.sub(r"\\d", "#", title.lower())')],
     ["reduced_to_what_does_not_change"], "a run of digits is one number"),
    ("title case kept", [('re.sub(r"\\d+", "#", title.lower())', 're.sub(r"\\d+", "#", title)')],
     ["reduced_to_what_does_not_change"], "Disk and disk are the same word"),
    ("number mark dropped", [('re.sub(r"[^a-z#]+", " ", text)', 're.sub(r"[^a-z]+", " ", text)')],
     ["reduced_to_what_does_not_change"], "SRV-1 and SRV-22 are the same driver and 12345 is not nothing"),
    ("empty title label dropped", [('.strip() or NONE_LABEL\n', ".strip()\n")],
     ["reduced_to_what_does_not_change"], "a title with nothing in it still has a group"),
    ("window end inclusive", [("return start <= moment.date() < end", "return start <= moment.date() <= end")],
     ["first_instant_and_excludes", "seeded_worlds"], "the window ends before its last day"),
    ("window start exclusive", [("return start <= moment.date() < end", "return start < moment.date() < end")],
     ["first_instant_and_excludes", "seeded_worlds"], "the window's first day is in it"),
    ("no company counted as a company", [("        if t.company is not None:\n            per[t.company] = per.get(t.company, 0) + 1", "        if True:\n            per[t.company] = per.get(t.company, 0) + 1")],
     ["no_company_is_never_a_repeat", "seeded_worlds"], "a ticket whose company is unknown is not known to repeat anything"),
    ("repeats counted without the first", [("return sum(n - 1 for n in per.values())", "return sum(n for n in per.values())")],
     ["are_grouped_by_category", "seeded_worlds"], "the first ticket of a company is not a repeat"),
    ("issue type label dropped", [("        sub = t.subcategory or NONE_LABEL", '        sub = t.subcategory or ""')],
     ["seeded_worlds"], "a ticket with no issue type is shown as such"),
    ("repeat percent of the wrong total", [("repeats * 100 // len(group)", "repeats * 100 // len(opened)")],
     ["are_grouped_by_category", "seeded_worlds"], "the percent is of the group's own tickets"),
    ("drivers ranked by count first", [("drivers.sort(key=lambda d: (-d.repeats, -d.tickets, d.category, d.subcategory, d.title))", "drivers.sort(key=lambda d: (-d.tickets, -d.repeats, d.category, d.subcategory, d.title))")],
     ["ranked_by_repeats", "seeded_worlds"], "a driver that repeats outranks one that is merely common"),
    ("category share of the wrong total", [("len(g) * 100 // len(opened)", "len(g) * 100 // (len(opened) + 1)")],
     ["are_grouped_by_category", "seeded_worlds"], "a share is of every ticket opened"),
    ("categories ranked by repeats first", [("shares.sort(key=lambda c: (-c.tickets, -c.repeats, c.category, c.subcategory))", "shares.sort(key=lambda c: (-c.repeats, -c.tickets, c.category, c.subcategory))")],
     ["seeded_worlds"], "the category list is ranked by volume"),
    ("top not applied", [("\"by_title\": [asdict(d) for d in drivers[:top]]", "\"by_title\": [asdict(d) for d in drivers[:top + 1]]")],
     ["ranked_by_repeats", "do_not_depend_on_the_order"], "the ranking is cut to the rows asked for"),
    ("first ticket chosen by number", [("ordered = sorted(group, key=lambda t: (t.opened_at, t.ticket_id))", "ordered = sorted(group, key=lambda t: t.ticket_id)")],
     ["seeded_worlds"], "the first ticket of a driver is the first one opened"),
    ("top not checked", [("if isinstance(top, bool) or not isinstance(top, int) or top < 1:", "if False:")],
     ["refuses_a_bad_window_or_top", "refuses_a_bad_top"], "a top of 0 or 2.5 is a typo"),
    ("empty window allowed", [("    if end <= start:\n        raise AnalysisError", "    if end < start:\n        raise AnalysisError")],
     ["refuses_a_bad_window_or_top"], "a window of no days is a caller bug"),
    ("window type not checked", [("    if not isinstance(start, date) or not isinstance(end, date) or isinstance(start, datetime) or isinstance(end, datetime):", "    if False:")],
     ["refuses_a_bad_window_or_top"], "a datetime is not a day"),
    # ------------------------------------------------------------------ alerts
    ("alert repeats miscounted", [("len(group) - distinct,", "distinct,")],
     ["grouped_by_source"], "a repeat is an alert beyond a device's first from that source"),
    ("alert share of the wrong total", [("len(group) * 100 // len(alerts)", "len(group) * 100 // len(group)")],
     ["grouped_by_source", "seeded_worlds"], "a share is of every active alert"),
    ("alert source label dropped", [("a.source_name or NONE_LABEL), []", 'a.source_name or ""), []')],
     ["grouped_by_source"], "an alert with no source name is shown as such"),
    ("alert sources ranked by name", [("drivers.sort(key=lambda d: (-d.alerts, -d.repeats, d.source_type, d.source_name))", "drivers.sort(key=lambda d: (-d.repeats, d.source_type, d.source_name))")],
     ["grouped_by_source", "seeded_worlds"], "the noisiest source comes first"),
    ("device list name ignored", [("label = names.get(did) or next(", "label = next(")],
     ["named_from_the_device_list"], "the device list is the first place a name is read"),
    ("offline counted the wrong way", [("sum(1 for d in devices if d.offline)", "sum(1 for d in devices if not d.offline)")],
     ["grouped_by_source"], "offline is offline"),
    ("noisy devices not ranked by sources", [("noisy.sort(key=lambda d: (-d.alerts, -d.sources, d.device_id))", "noisy.sort(key=lambda d: (-d.alerts, d.device_id))")],
     ["seeded_worlds", "do_not_depend_on_the_order"], "of two devices with as many alerts, the one with more kinds comes first"),
    # ------------------------------------------------------------------ SLA risk
    ("as_of zone not checked", [('    as_of = utc(as_of, "as_of")\n    _check_window(window_start, window_end)\n    _check_top(top)\n    if isinstance(horizon_hours', '    _check_window(window_start, window_end)\n    _check_top(top)\n    if isinstance(horizon_hours')],
     ["refuses_a_bad_input"], "a time with no zone is a guess"),
    ("closed at as_of still open", [("(done is None or done > as_of)", "(done is None or done >= as_of)")],
     ["judged_at_as_of", "seeded_worlds"], "work finished at this very second is done"),
    ("finished later still open dropped", [("(done is None or done > as_of)", "(done is None)")],
     ["judged_at_as_of", "seeded_worlds"], "a ticket finished after as_of was open at as_of"),
    ("goal at as_of breached", [('"breached" if due < as_of else', '"breached" if due <= as_of else')],
     ["breached_at_risk_ok", "seeded_worlds"], "a goal this very second is not yet missed"),
    ("goal at the horizon at risk", [('"at_risk" if due < horizon else', '"at_risk" if due <= horizon else')],
     ["breached_at_risk_ok", "seeded_worlds"], "a goal exactly at the horizon is outside it"),
    ("horizon one hour long", [("horizon = as_of + timedelta(hours=horizon_hours)", "horizon = as_of + timedelta(hours=horizon_hours + 1)")],
     ["horizon_is_the_one_asked_for", "seeded_worlds"], "the horizon is the hours asked for"),
    ("first response answered strictly before", [("t.first_response_at <= as_of", "t.first_response_at < as_of")],
     ["first_response_is_overdue", "seeded_worlds"], "answered at this very second is answered"),
    ("first response due now is overdue", [("t.first_response_due_at < as_of", "t.first_response_due_at <= as_of")],
     ["first_response_is_overdue", "seeded_worlds"], "due this very second is not yet overdue"),
    ("oldest is the youngest", [('g["oldest"] = max(g["oldest"], hours_between(t.opened_at, as_of))', 'g["oldest"] = min(g["oldest"], hours_between(t.opened_at, as_of))')],
     ["oldest_open_ticket", "seeded_worlds"], "the oldest open ticket is the one open longest"),
    ("groups ranked by open first", [("rows.sort(key=lambda r: (-r.breached, -r.at_risk, -r.open, r.category, r.priority))", "rows.sort(key=lambda r: (-r.open, -r.breached, -r.at_risk, r.category, r.priority))")],
     ["groups_are_ranked", "seeded_worlds"], "breached work outranks merely large queues"),
    ("priority label dropped", [("(t.category, t.priority or NONE_LABEL)", '(t.category, t.priority or "")')],
     ["groups_are_ranked", "seeded_worlds"], "a ticket with no priority is shown as such"),
    ("late counted at the goal", [("finished_late += 1 if done > t.resolution_due_at else 0", "finished_late += 1 if done >= t.resolution_due_at else 0")],
     ["finished_in_the_window_are_late", "seeded_worlds"], "finishing on the goal is on time"),
    ("finished outside the window counted", [("        if done is not None and _in_window(done, window_start, window_end):", "        if done is not None:")],
     ["finished_in_the_window_are_late", "flow_is_opened", "seeded_worlds"], "only tickets finished in the window count for it"),
    ("late percent of the wrong total", [("finished_late * 100 // finished_with_goal", "finished_late * 100 // (finished_with_goal + 1)")],
     ["finished_in_the_window_are_late", "seeded_worlds"], "the percent is of the tickets with a goal"),
    ("late percent with no goals", [("None if finished_with_goal == 0 else", "0 if finished_with_goal == 0 else")],
     ["finished_in_the_window_are_late", "seeded_worlds"], "no tickets with a goal is not 0 percent late"),
    ("flow ranked by volume", [("flows.sort(key=lambda f: (-f.net, -f.opened, f.category))", "flows.sort(key=lambda f: (-f.opened, -f.net, f.category))")],
     ["flow_is_opened", "seeded_worlds"], "the category filling up fastest comes first"),
    ("idle categories listed", [("for c, (o, f) in flow.items() if o or f]", "for c, (o, f) in flow.items()]")],
     ["flow_is_opened", "seeded_worlds"], "a category with nothing in the window is not a row"),
    ("horizon not checked", [("horizon_hours < 1:", "horizon_hours < 0:")],
     ["refuses_a_bad_input"], "a horizon of 0 hours sees nothing"),
    # ------------------------------------------------------------------ touch time
    ("tickets matched across vendors", [("category_of = {(t.source, t.ticket_id): t.category for t in tickets}", "category_of = {(\"\", t.ticket_id): t.category for t in tickets}")],
     ["spread_over_the_category", "seeded_worlds"], "ticket 1 of one system is not ticket 1 of another"),
    ("no ticket counted as unmatched", [("        if e.ticket_id is None:\n            non_ticket += e.seconds\n            continue", "        if e.ticket_id is None:\n            unmatched += e.seconds\n            continue")],
     ["spread_over_the_category", "seeded_worlds"], "time on no ticket is not time on a missing ticket"),
    ("mean taken per entry", [('c["seconds"] // len(c["tickets"])', 'c["seconds"] // c["entries"]')],
     ["spread_over_the_category", "seeded_worlds"], "the mean is per ticket"),
    ("most time tie by entry order", [('key=lambda kv: (-kv[1], kv[0]))[0]', "key=lambda kv: -kv[1])[0]")],
     ["most_time_tie"], "a tie goes to the lowest ticket number, whatever order the entries came in"),
    ("categories ranked by name", [("rows.sort(key=lambda r: (-r.seconds, r.category))\n    placed", "rows.sort(key=lambda r: (r.category, -r.seconds))\n    placed")],
     ["spread_over_the_category", "seeded_worlds"], "the category that takes most time comes first"),
    ("entries outside the window counted", [("in_window = [e for e in times if _in_window(e.worked_on, window_start, window_end)]", "in_window = list(times)")],
     ["utc_day_it_was_worked", "seeded_worlds"], "only time worked in the window counts"),
    ("total leaves out unplaced time", [('"total_seconds": placed + non_ticket + unmatched', '"total_seconds": placed')],
     ["spread_over_the_category", "seeded_worlds"], "every second is accounted for once"),
    ("entries miscounted", [('c["entries"] += 1', 'c["entries"] += 2')],
     ["spread_over_the_category", "seeded_worlds"], "each entry is one"),
    ("technician tickets miscounted", [('tech["tickets"].add(e.ticket_id)', 'tech["tickets"].add(e.entry_id)')],
     ["spread_over_the_category", "seeded_worlds"], "a technician touched each ticket once however many entries"),
    # ------------------------------------------------------------------ the report and its text
    ("notes for absent vendors", [('    if "connectwise" in sources:', "    if True:")],
     ["every_ranking_what_was_read", "none_and_does_not_fail"], "a note about a vendor that was not read is noise"),
    ("hours shown from the wrong remainder", [("seconds % 3600 * 100 // 3600", "seconds % 3000 * 100 // 3600")],
     ["hours_are_shown_as_hours"], "hours and hundredths, rounded down"),
    ("empty table not said", [('return ["_none_", ""]', "return []")],
     ["none_and_does_not_fail"], "an empty ranking says so"),
    ("no goal late line dropped", [('if sr["finished_late_percent"] is None else', 'if False else')],
     ["none_and_does_not_fail"], "no tickets with a goal has no late percent to print"),
    ("time shown without its zone", [('strftime("%Y-%m-%dT%H:%M:%SZ")', 'strftime("%Y-%m-%dT%H:%M:%S")')],
     ["ticket_drivers_are_grouped", "the_markdown_carries", "defaults_the_time"], "a time in the report is UTC and says so"),
])

mutations("ledger", [
    ("merge accepts a repeated record", [('if ident in seen:\n                raise LedgerError("%s %s is in more than one ledger"', 'if False:\n                raise LedgerError("%s %s is in more than one ledger"')],
     ["same_record_in_two_ledgers", "repeated_ledger"], "a record in two ledgers would count twice"),
    ("merge as young as the youngest", [("merged.generated_at = min(stamps) if stamps else None", "merged.generated_at = max(stamps) if stamps else None")],
     ["as_old_as_the_oldest"], "a merged ledger is only as current as its oldest part"),
    ("merge loses the header time", [("    ledger.generated_at = _parse(header.get(\"generated_at\"))\n", "")],
     ["defaults_the_time", "as_old_as_the_oldest"], "the report is judged at the time the ledger was written"),
])

# ---------------------------------------------------------------------- shared helpers and records
mutations("common", [
    ("hours rounded down", [("quantize(Decimal(1), rounding=ROUND_HALF_UP)", "quantize(Decimal(1), rounding=ROUND_DOWN)")],
     ["time_entries_become_records", "seeded_worlds"], "half a second rounds up, as every amount in this hub does"),
    ("hours upper bound removed", [("if hours < 0 or hours > maximum_hours:", "if hours < 0:")],
     ["malformed_time_entry"], "an entry above a day is a broken answer"),
    ("negative hours allowed", [("if hours < 0 or hours > maximum_hours:", "if hours > maximum_hours:")],
     ["malformed_time_entry"], "time below zero is a broken answer"),
    ("naive time accepted for a vendor that never says UTC", [("if zone is None and not naive_is_utc:", "if False:")],
     ["malformed_ticket", "malformed_time_entry"], "a time with no zone is a guess unless the vendor says it is UTC"),
    ("offset sign ignored", [('sign = 1 if zone[0] == "+" else -1', "sign = 1")],
     ["seeded_worlds", "offset_are_read"], "a time written in New York is not the same instant as one written in Manila"),
    ("impossible offset allowed", [("if hours > 23 or minutes > 59:", "if False:")],
     ["malformed_ticket"], "a +25:00 offset is a broken date"),
    ("date shape not checked", [("if m is None:", "if False:")],
     ["malformed_ticket"], "text that is not a date must not crash the sync with a type error"),
    ("calendar check removed", [('except ValueError:\n        raise UpstreamBroken("%s is not a calendar date and time" % what) from None', 'except ZeroDivisionError:\n        raise UpstreamBroken("%s is not a calendar date and time" % what) from None')],
     ["malformed_ticket"], "month 13 is not a month"),
    ("fraction of a second dropped", [('(fraction or "")[:7]', '""')],
     ["fraction_of_a_second"], "the fraction the vendor sends is kept"),
    ("non text date allowed", [("if not isinstance(value, str):\n        raise UpstreamBroken(\"%s is %s, not a date and time\"", "if False:\n        raise UpstreamBroken(\"%s is %s, not a date and time\"")],
     ["malformed_ticket"], "a number is not a date"),
    ("plain http allowed to anywhere", [('if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):', "if False:")],
     ["api_address_that_is_not"], "a key sent over plain http leaves in the clear"),
    ("address user info allowed", [("if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:", "if not parsed.hostname:")],
     ["api_address_that_is_not"], "a user name in the address would be sent as a credential"),
    ("bare address path allowed", [("if not allow_path and path:", "if False:")],
     ["base_address_that_is_not", "address_that_is_not_https"], "an address that must be bare cannot carry a path"),
    ("address dotdot allowed", [('if ".." in path or "//" in path:', "if False:")],
     ["api_address_that_is_not"], "a path that climbs is another endpoint"),
    ("window one day too long", [("for i in range((end - start).days)]", "for i in range((end - start).days + 1)]")],
     ["four_counts", "seeded_worlds"], "a half open window ends before its end"),
])
mutations("records", [
    ("instants kept in their own zone", [("    return value.astimezone(timezone.utc)", "    return value")],
     ["same_instant_in_utc", "utc_day_of_a_ticket", "seeded_worlds"], "a time written in Manila is the same instant in UTC, and its day is the UTC day"),
    ("resolved ticket still open", [("return not self.closed and self.resolved_at is None", "return not self.closed")],
     ["a_ticket_becomes_a_record"], "a ticket resolved but not closed is no longer backlog"),
    ("closed date without closed allowed", [("if self.closed_at is not None and not self.closed:", "if False:")],
     ["refuses_each_defect"], "a closed date on an open ticket is a contradiction"),
])


# ---------------------------------------------------------------------- the ledger file
mutations("ledger", [
    ("version not checked", [('if header.get("version") != VERSION:', "if False:")],
     ["damaged_ledger"], "a file written by a newer version must not be read as this one"),
    ("header name not checked", [('if not isinstance(header, dict) or header.get("ledger") != "msp-ledger":', "if False:")],
     ["damaged_ledger"], "a file that is not a ledger must not be read as one"),
    ("unknown field accepted", [("if extra:", "if False:")],
     ["damaged_ledger"], "a field this version does not know is a different file"),
    ("missing field accepted", [("if missing:", "if False:")],
     ["damaged_ledger"], "a record with a field gone is damaged, not defaulted"),
    ("duplicate read accepted", [('if ident in seen:\n            raise LedgerError("line %d repeats', 'if False:\n            raise LedgerError("line %d repeats')],
     ["damaged_ledger"], "a record twice would count twice"),
    ("header count not checked", [('if header.get("records") != len(lines) - 1:', "if False:")],
     ["damaged_ledger"], "a file cut short must not read as a smaller ledger"),
    ("zone not required on read", [('if not isinstance(value, str) or not value.endswith("Z"):', "if False:")],
     ["damaged_ledger"], "a time with no zone is a guess"),
    ("duplicate write accepted", [('if key in seen:\n            raise LedgerError("%s %s appears twice"', 'if False:\n            raise LedgerError("%s %s appears twice"')],
     ["appearing_twice"], "a record twice would count twice"),
    ("records not sorted", [('rows = sorted((_dump(r) for r in records), key=lambda r: (r["kind"], str(r["source"]), str(r[_ID[r["kind"]]])))', "rows = [_dump(r) for r in records]")],
     ["same_bytes_in_any_order"], "the same records must give the same file"),
    ("header count wrong", [('"records": len(rows)}', '"records": len(rows) + 1}')],
     ["round_trips", "damaged_ledger"], "the header count is what shows a cut file"),
    ("some dates not parsed", [('if value is not None and (key.endswith("_at") or key in ("worked_on", "last_contact")):', 'if value is not None and key.endswith("_at"):')],
     ["round_trips"], "every date field must come back as a date"),
])


def _clean_bytecode() -> None:
    for cache in ROOT.joinpath("app").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def suites_of(target: str) -> list[str]:
    return [s for s in TARGETS[target][1] if (ROOT / s).exists()]


def run_suite(target: str, timeout: int = MUTANT_TIMEOUT_SECONDS, stop_first: bool = True):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    _clean_bytecode()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [str(PY), "-B", "-m", "pytest", *suites_of(target), "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"] + (["-x"] if stop_first else [])
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def missing_targets(only: str | None = None) -> list[str]:
    """Guards whose old text is gone from its file, or that change nothing. Such a guard proves nothing."""
    texts: dict[str, str] = {}
    problems = []
    for target, name, edits, *_ in MUTATIONS:
        if only and target != only:
            continue
        path = TARGETS[target][0]
        if target not in texts:
            if not path.exists():
                problems.append(f"{name}: {path.name} does not exist")
                continue
            texts[target] = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        for old, new in edits:
            if texts[target].count(old) != 1:
                problems.append(f"{name}: its target text appears {texts[target].count(old)} times in {path.name}, it must appear once")
            elif old == new:
                problems.append(f"{name}: the mutation changes nothing")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--check-targets", action="store_true", help="only check that every guard finds its target text, edit nothing")
    parser.add_argument("--match", help="comma separated words; only guards whose name holds one run")
    parser.add_argument("--only", choices=sorted(TARGETS), help="only the guards of one target")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems = missing_targets(args.only)
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    chosen = [m for m in MUTATIONS if not args.only or m[0] == args.only]
    if args.check_targets:
        print(f"all {len(chosen)} guards find their target")
        return 0

    match = args.match.split(",") if args.match else None
    used = sorted({m[0] for m in chosen})
    originals = {t: TARGETS[t][0].read_bytes() for t in used}

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass

    killed = survived = wrong_test = 0
    try:
        for target in used:
            green, _ = run_suite(target, BASELINE_TIMEOUT_SECONDS)
            if green is not True:
                print(f"baseline suite for {TARGETS[target][0].name} is RED or hung, fix that before mutating")
                return 1
        print("baseline: green")
        ran = 0
        for target, name, edits, expected, why in chosen:
            if match and not any(word in name for word in match):
                continue
            ran += 1
            path = TARGETS[target][0]
            original = originals[target]
            crlf = b"\r\n" in original
            text = original.decode("utf-8").replace("\r\n", "\n")
            mutated = text
            for old, new in edits:
                mutated = mutated.replace(old, new, 1)
            assert mutated != text, f"{name}: the mutation changed nothing, so a green result would mean nothing"
            if crlf:
                mutated = mutated.replace("\n", "\r\n")
            try:
                path.write_bytes(mutated.encode("utf-8"))
                green, failed = run_suite(target, stop_first=False)
            finally:
                path.write_bytes(original)
            if green is None:
                print(f"[ok]   {name}: suite HUNG, so the guard also stops an endless loop")
                killed += 1
            elif green:
                print(f"[FAIL] {name}: suite stayed GREEN, so nothing tests that {why}")
                survived += 1
            elif any(frag in test for test in failed for frag in expected):
                print(f"[ok]   {name}: failed {len(failed)} test(s), including the expected one")
                killed += 1
            else:
                print(f"[FAIL] {name}: the suite went red but not in {expected}; failed: {failed[:4]}")
                wrong_test += 1

        print()
        for target in used:
            green, _ = run_suite(target, BASELINE_TIMEOUT_SECONDS)
            if green is not True:
                print("suite is RED or hung after restoring, a mutation leaked")
                return 1
        print("source restored, suites green again")
        print(f"mutants run {ran}: killed {killed}, survived {survived}, killed by the wrong test {wrong_test}")
        bad = survived + wrong_test
        print(f"mutation control: {'PASS' if bad == 0 else str(bad) + ' UNTESTED GUARD(S)'}")
        return 1 if bad else 0
    finally:
        for target, original in originals.items():
            path = TARGETS[target][0]
            if path.read_bytes() != original:
                path.write_bytes(original)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
