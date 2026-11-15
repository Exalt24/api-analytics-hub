"""The REST reader against the stand-in server: paging, the totals check, the address refusals, the bounded retry, and what lands in bronze. No Spark needed
except where a landing is checked."""
from __future__ import annotations

import pytest

from lakehouse.rest_client import RestError, fetch_query, lead_query, soql_datetime
from lakehouse.sample_data import sf_id
from lakehouse.stand_in import StandInSalesforce
from lakehouse.tests.helpers import land


def leads(n: int) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"Id": sf_id("00Q", i), "CreatedDate": f"2026-10-{1 + i % 28:02d}T08:00:00.000+0000", "LastModifiedDate": f"2026-10-{1 + i % 28:02d}T09:{i % 60:02d}:00.000+0000",
                    "LeadSource": "Web", "Status": "Open - Not Contacted", "IsConverted": False, "ConvertedDate": None})
    return out


def read(si, **kw):
    return fetch_query(si.url, kw.pop("token", "stand-in-token"), kw.pop("soql", lead_query(None)), backoff=0.01, **kw)


def test_every_page_is_read_once_in_order_and_the_total_matches():
    with StandInSalesforce(leads(23)) as si:
        got = read(si, batch_size=7)
    assert got.pages == 4 and got.api_calls == 4 and len(got.records) == 23
    assert len({r["Id"] for r in got.records}) == 23
    assert all(r["attributes"]["type"] == "Lead" for r in got.records)


def test_a_since_filter_returns_only_what_changed_after_it():
    data = leads(30)
    with StandInSalesforce(data) as si:
        got = read(si, soql=lead_query(soql_datetime("2026-10-20T00:00:00.000+0000")))
    expected = [d for d in data if d["LastModifiedDate"] >= "2026-10-20"]
    assert len(got.records) == len(expected) and 0 < len(expected) < 30


def test_a_datetime_text_becomes_an_unquoted_soql_literal():
    assert soql_datetime("2026-10-01T12:34:56.000+0000") == "2026-10-01T12:34:56Z"
    assert soql_datetime("2026-10-01T20:34:56.000+0800") == "2026-10-01T12:34:56Z"
    assert "WHERE LastModifiedDate >= 2026-10-01T12:34:56Z" in lead_query("2026-10-01T12:34:56Z") and "'" not in lead_query("2026-10-01T12:34:56Z")


def test_a_bad_token_is_refused_at_once_with_no_retry():
    with StandInSalesforce(leads(3)) as si:
        with pytest.raises(RestError, match="HTTP 401"):
            read(si, token="wrong")
        assert si.calls == 1


def test_a_503_is_retried_a_bounded_number_of_times():
    with StandInSalesforce(leads(3)) as si:
        si.http_errors = 2
        assert len(read(si, retries=2).records) == 3
    with StandInSalesforce(leads(3)) as si:
        si.http_errors = 50
        with pytest.raises(RestError, match="HTTP 503"):
            read(si, retries=2)
        assert si.calls == 3  # the first try and two retries, then it gives up


def test_a_next_page_address_off_the_data_api_is_refused_so_the_token_stays_put():
    with StandInSalesforce(leads(10)) as si:
        si.next_url_override = "http://evil.example/steal"
        with pytest.raises(RestError, match="refusing next page address"):
            read(si, batch_size=4)


def test_a_next_page_address_that_repeats_is_refused_as_a_loop():
    with StandInSalesforce(leads(10)) as si:
        si.next_url_override = "/services/data/v62.0/query/loop-0"
        with pytest.raises(RestError):
            read(si, batch_size=4)


def test_a_total_that_does_not_match_what_arrived_is_an_error():
    with StandInSalesforce(leads(10)) as si:
        si.total_override = 99
        with pytest.raises(RestError, match="totalSize says 99"):
            read(si, batch_size=4)


def test_a_page_that_is_not_json_is_kept_as_evidence_and_the_read_fails():
    with StandInSalesforce(leads(10)) as si:
        si.corrupt_page = 2
        with pytest.raises(RestError, match="page 2 is not valid JSON") as err:
            read(si, batch_size=4)
    assert len(err.value.records) == 4 and err.value.rejects and err.value.rejects[0][0].startswith('{"totalSize": 3')


def test_an_unknown_field_is_the_orgs_malformed_query_error():
    with StandInSalesforce(leads(3)) as si:
        with pytest.raises(RestError, match="INVALID_FIELD"):
            read(si, soql=lead_query(None, ("Id", "Region__c")))


def test_overlapping_pages_land_each_lead_once(lh):
    with StandInSalesforce(leads(12)) as si:
        si.duplicate_on_page = True
        got = read(si, batch_size=4)
    assert len(got.records) == 12 + got.pages - 1
    counts = land(lh, [(r, "00DST0000000001AAA") for r in got.records], table="bronze_sf_lead", source_table="lead", source_name="salesforce_rest")
    assert counts["landed"] == 12 and counts["skipped_known"] == got.pages - 1 and counts["read"] == len(got.records)
