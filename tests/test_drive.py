"""The Google Drive delivery step, tested against a stand-in HTTP server on localhost (no network, no database, no Google account).

Every behaviour has a test and, where a test could pass for the wrong reason, a control that shows it can fail. tests/mutation_check_drive.py
breaks each guard in app/report/drive.py and requires the matching test here to go red. The stand-in was written from Google's Drive API
documentation, so a green run says the code matches that reading. The one real run against a personal Drive is in docs/test-runs/.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.report import drive  # noqa: E402
from drive_stand_in import StandIn  # noqa: E402

# Built from pieces so no whole token sits in this file for the repo's secret scan to flag.
ACCESS = "ya29." + "standin-" + "access"
REFRESH = "1//" + "standin-" + "refresh"
CLIENT_ID = "standin-" + "client"
CLIENT_SECRET = "standin-" + "secret"
DECK = b"PK\x03\x04 a made-up deck " + bytes(range(200)) * 5
DECK_B = b"PK\x03\x04 the same month rebuilt " + bytes(range(100)) * 7


class Sleeps:
    """Stands in for time.sleep: records the delays instead of waiting."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(scope="module")
def server():
    stand_in = StandIn(ACCESS, REFRESH, CLIENT_ID, CLIENT_SECRET).start()
    yield stand_in
    stand_in.stop()


@pytest.fixture()
def si(server):
    server.reset()
    return server


@pytest.fixture()
def folder(si):
    return si.add_folder()


@pytest.fixture()
def sleeps():
    return Sleeps()


@pytest.fixture()
def client(si, sleeps):
    return drive.DriveClient(drive.StaticToken(ACCESS), si.url, sleep=sleeps)


def uploads(si, method=None):
    return [r for r in si.requests if r["path"].startswith("/upload/") and (method is None or r["method"] == method)]


def write_token_file(tmp_path, si, access="expired", expiry="2020-01-01T00:00:00Z", token_uri=None):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "token": access, "refresh_token": REFRESH, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "token_uri": token_uri or si.url + "/token", "expiry": expiry,
    }), encoding="utf-8")
    return path


# ---------------------------------------------------------------- names


def test_the_file_name_is_stable_for_a_tenant_and_month():
    assert drive.deck_name("2026-10", "Acme Outfitters") == "monthly-report-acme-outfitters-2026-10.pptx"
    assert drive.deck_name("2026-10", "Acme Outfitters") == drive.deck_name("2026-10", "Acme Outfitters")
    assert drive.deck_name("2026-10") == "monthly-report-2026-10.pptx"
    assert drive.deck_name("2026-10", "  ../x'y ") == "monthly-report-x-y-2026-10.pptx"


def test_a_query_string_escapes_a_backslash_and_an_apostrophe():
    assert drive.q_escape("it's a\\b") == "it\\'s a\\\\b"


# ---------------------------------------------------------------- create, replace, unchanged


def test_a_new_deck_is_created_in_the_folder_by_a_multipart_upload(si, folder, client):
    done = client.deliver(DECK, "report.pptx", folder)
    assert (done.action, done.name, done.upload, done.size, done.folder_id) == ("created", "report.pptx", "multipart", len(DECK), folder)
    (stored,) = si.in_folder(folder)
    assert stored["name"] == "report.pptx" and stored["content"] == DECK and stored["id"] == done.file_id
    (req,) = uploads(si, "POST")
    assert req["query"]["uploadType"] == "multipart" and req["content_type"].startswith("multipart/related; boundary=")
    assert req["auth"] == "Bearer " + ACCESS


def test_the_multipart_body_puts_the_metadata_first_and_the_media_second():
    body, ctype = drive.DriveClient._multipart({"name": "a.pptx", "parents": ["f1"]}, b"BYTES", "application/x-test")
    boundary = ctype.split("boundary=")[1].encode()
    parts = body.split(b"--" + boundary)
    assert parts[1].lstrip().startswith(b"Content-Type: application/json; charset=UTF-8") and b'"name": "a.pptx"' in parts[1]
    assert b"Content-Type: application/x-test" in parts[2] and b"BYTES" in parts[2]
    assert body.endswith(b"--" + boundary + b"--")


def test_the_second_run_with_the_same_bytes_sends_nothing(si, folder, client):
    first = client.deliver(DECK, "report.pptx", folder)
    before = len(uploads(si))
    again = client.deliver(DECK, "report.pptx", folder)
    assert again.action == "unchanged" and again.file_id == first.file_id and len(uploads(si)) == before == 1


def test_a_rebuilt_deck_replaces_the_content_of_the_same_file(si, folder, client):
    first = client.deliver(DECK, "report.pptx", folder)
    again = client.deliver(DECK_B, "report.pptx", folder)
    assert again.action == "replaced" and again.file_id == first.file_id
    (stored,) = si.in_folder(folder)
    assert stored["content"] == DECK_B
    assert [r["method"] for r in uploads(si)] == ["POST", "PATCH"] and uploads(si, "PATCH")[0]["path"].endswith("/" + first.file_id)


def test_reruns_never_leave_a_second_copy(si, folder, client):
    for content in (DECK, DECK, DECK_B, DECK_B, DECK, DECK_B):
        client.deliver(content, "report.pptx", folder)
        assert len(si.in_folder(folder, "report.pptx")) == 1


def test_two_months_are_two_files_not_one_overwritten(si, folder, client):
    client.deliver(DECK, "monthly-report-2026-09.pptx", folder)
    client.deliver(DECK_B, "monthly-report-2026-10.pptx", folder)
    assert sorted(f["name"] for f in si.in_folder(folder)) == ["monthly-report-2026-09.pptx", "monthly-report-2026-10.pptx"]


def test_an_existing_duplicate_is_reported_and_the_oldest_is_the_one_replaced(si, folder, client):
    old = si.add_file("report.pptx", folder, b"old")
    newer = si.add_file("report.pptx", folder, b"newer")
    done = client.deliver(DECK, "report.pptx", folder)
    assert done.file_id == old and done.duplicates == 1
    assert si.files[old]["content"] == DECK and si.files[newer]["content"] == b"newer"  # the other copy is left alone, never deleted


def test_a_file_in_the_trash_with_the_same_name_is_ignored(si, folder, client):
    si.add_file("report.pptx", folder, b"binned", trashed=True)
    done = client.deliver(DECK, "report.pptx", folder)
    assert done.action == "created" and len(si.in_folder(folder)) == 1


def test_the_same_name_in_another_folder_is_not_touched(si, folder, client):
    other = si.add_folder("elsewhere")
    mine = si.add_file("report.pptx", other, b"not ours")
    client.deliver(DECK, "report.pptx", folder)
    assert si.files[mine]["content"] == b"not ours" and len(si.in_folder(folder)) == 1


def test_a_name_with_an_apostrophe_and_a_backslash_is_found_again(si, folder, client):
    name = "Owner's \\ report.pptx"
    first = client.deliver(DECK, name, folder)
    again = client.deliver(DECK, name, folder)
    assert again.action == "unchanged" and again.file_id == first.file_id and len(si.in_folder(folder)) == 1


def test_the_search_follows_next_page_token(si, folder, client):
    si.page_size_cap = 2
    for _ in range(5):
        si.add_file("report.pptx", folder, b"x")
    done = client.deliver(DECK, "report.pptx", folder)
    assert done.duplicates == 4
    assert len([r for r in si.requests if r["path"] == "/drive/v3/files" and r["query"].get("pageToken")]) == 2


def test_a_bytes_round_trip_through_download_and_delete(si, folder, client):
    done = client.deliver(DECK, "report.pptx", folder)
    assert client.download(done.file_id) == DECK
    client.delete(done.file_id)
    assert si.in_folder(folder) == []
    with pytest.raises(drive.DriveError) as e:
        client.get(done.file_id)
    assert e.value.status == 404


# ---------------------------------------------------------------- the folder


def test_a_folder_id_that_does_not_exist_is_one_clear_error_and_nothing_is_uploaded(si, client):
    with pytest.raises(drive.DriveError, match="not found") as e:
        client.deliver(DECK, "report.pptx", "nope")
    assert e.value.status == 404 and uploads(si) == []


def test_a_file_id_given_as_the_folder_is_refused(si, folder, client):
    not_a_folder = si.add_file("x.pptx", folder, b"x")
    with pytest.raises(drive.DriveError, match="not a folder") as e:
        client.deliver(DECK, "report.pptx", not_a_folder)
    assert e.value.reason == "notAFolder" and uploads(si) == []


def test_a_folder_in_the_trash_is_refused(si, client):
    f = si.add_folder()
    si.files[f]["trashed"] = True
    with pytest.raises(drive.DriveError, match="trash"):
        client.deliver(DECK, "report.pptx", f)


# ---------------------------------------------------------------- resumable uploads


def test_a_deck_over_five_megabytes_uses_a_resumable_upload(si, folder, client, monkeypatch):
    monkeypatch.setattr(drive, "MULTIPART_LIMIT", 100)
    done = client.deliver(DECK, "report.pptx", folder)
    assert done.upload == "resumable" and si.in_folder(folder)[0]["content"] == DECK
    start, put = uploads(si, "POST")[0], uploads(si, "PUT")[0]
    assert start["query"]["uploadType"] == "resumable" and put["path"].startswith("/upload/session/")


def test_a_deck_at_exactly_the_limit_is_still_multipart(si, folder, client, monkeypatch):
    monkeypatch.setattr(drive, "MULTIPART_LIMIT", len(DECK))
    assert client.deliver(DECK, "report.pptx", folder).upload == "multipart"


def test_resumable_can_be_asked_for_on_a_small_file(si, folder, client):
    assert client.deliver(DECK, "report.pptx", folder, upload="resumable").upload == "resumable"


def test_a_resumable_replace_uses_patch_on_the_same_file(si, folder, client):
    first = client.deliver(DECK, "report.pptx", folder, upload="resumable")
    again = client.deliver(DECK_B, "report.pptx", folder, upload="resumable")
    assert again.action == "replaced" and again.file_id == first.file_id and uploads(si, "PATCH")[0]["query"]["uploadType"] == "resumable"
    assert si.in_folder(folder)[0]["content"] == DECK_B


def test_an_interrupted_resumable_upload_resumes_from_the_byte_the_server_reports(si, folder, client):
    si.faults = ["PUT:interrupt"]
    done = client.deliver(DECK, "report.pptx", folder, upload="resumable")
    assert done.action == "created" and si.in_folder(folder)[0]["content"] == DECK and len(si.in_folder(folder)) == 1
    puts = uploads(si, "PUT")
    assert puts[1]["content_range"] == "bytes */%d" % len(DECK)  # "an empty PUT with Content-Range: bytes */total"
    half = len(DECK) // 2
    assert puts[2]["content_range"] == "bytes %d-%d/%d" % (half, len(DECK) - 1, len(DECK))
    assert puts[2]["content_length"] == len(DECK) - half


def test_an_expired_upload_session_starts_the_whole_step_over_and_still_makes_one_file(si, folder, client):
    si.faults = ["PUT:interrupt"]
    si.forget_sessions = True
    done = client.deliver(DECK, "report.pptx", folder, upload="resumable")
    assert done.action == "created" and len(si.in_folder(folder)) == 1 and si.in_folder(folder)[0]["content"] == DECK


# ---------------------------------------------------------------- retries


def test_a_429_waits_for_retry_after_then_succeeds(si, folder, client, sleeps):
    si.faults, si.retry_after = ["429"], "3"
    assert client.deliver(DECK, "report.pptx", folder).action == "created" and sleeps.calls == [3.0]


def test_a_huge_retry_after_is_capped(si, folder, client, sleeps):
    si.faults, si.retry_after = ["429"], "86400"
    client.deliver(DECK, "report.pptx", folder)
    assert sleeps.calls == [drive.MAX_RETRY_AFTER]


def test_a_5xx_is_retried_with_a_doubling_delay(si, folder, client, sleeps):
    si.faults = ["503", "500"]
    assert client.deliver(DECK, "report.pptx", folder).action == "created"
    assert sleeps.calls == [1.0, 2.0]


def test_the_delay_between_tries_is_capped(si, folder, sleeps):
    c = drive.DriveClient(drive.StaticToken(ACCESS), si.url, sleep=sleeps, max_attempts=8, backoff_cap=4.0)
    si.faults = ["503"] * 6
    c.deliver(DECK, "report.pptx", folder)
    assert sleeps.calls == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]


def test_a_403_rate_limit_is_retried(si, folder, client, sleeps):
    si.faults = ["403rate"]
    assert client.deliver(DECK, "report.pptx", folder).action == "created" and len(sleeps.calls) == 1


def test_a_403_that_is_not_a_rate_limit_is_not_retried(si, folder, client, sleeps):
    si.faults = ["403perm"]
    with pytest.raises(drive.DriveError) as e:
        client.deliver(DECK, "report.pptx", folder)
    assert e.value.status == 403 and e.value.reason == "insufficientFilePermissions" and not e.value.retryable and sleeps.calls == []


def test_a_404_is_not_retried(si, client, sleeps):
    with pytest.raises(drive.DriveError) as e:
        client.deliver(DECK, "report.pptx", "nope")
    assert e.value.retryable is False and sleeps.calls == []


def test_the_retries_stop_after_the_attempt_limit_and_say_why(si, folder, client, sleeps):
    si.faults = ["503"] * 10
    with pytest.raises(drive.DriveError) as e:
        client.deliver(DECK, "report.pptx", folder)
    assert e.value.status == 503 and e.value.retryable and len(sleeps.calls) == client.max_attempts - 1
    assert si.in_folder(folder) == [] and len(si.faults) == 10 - client.max_attempts


def test_a_lost_answer_to_a_create_is_found_by_name_and_not_created_twice(si, folder, client, sleeps):
    si.faults = ["POST:drop_after"]  # the file is created, then the connection closes with no answer
    done = client.deliver(DECK, "report.pptx", folder)
    (stored,) = si.in_folder(folder)
    assert stored["content"] == DECK and done.file_id == stored["id"] and done.action == "unchanged" and len(sleeps.calls) == 1


def test_a_connection_dropped_before_the_request_is_retried(si, folder, client):
    si.faults = ["GET:drop_before"]
    assert client.deliver(DECK, "report.pptx", folder).action == "created"


def test_a_lost_answer_to_a_replace_ends_with_one_file_holding_the_new_bytes(si, folder, client):
    client.deliver(DECK, "report.pptx", folder)
    si.faults = ["PATCH:drop_after"]
    done = client.deliver(DECK_B, "report.pptx", folder)
    (stored,) = si.in_folder(folder)
    assert stored["content"] == DECK_B and done.file_id == stored["id"]


# ---------------------------------------------------------------- credentials


def test_a_fixed_token_that_is_refused_is_a_clear_error(si, folder, sleeps):
    c = drive.DriveClient(drive.StaticToken("wrong"), si.url, sleep=sleeps)
    with pytest.raises(drive.DriveError) as e:
        c.deliver(DECK, "report.pptx", folder)
    assert e.value.status == 401 and not e.value.retryable and sleeps.calls == []


def test_an_expired_token_file_is_renewed_with_the_refresh_grant_before_the_first_call(si, folder, tmp_path, sleeps):
    c = drive.DriveClient(drive.OAuthTokenFile(write_token_file(tmp_path, si)), si.url, sleep=sleeps)
    assert c.deliver(DECK, "report.pptx", folder).action == "created"
    assert si.refreshes == 1 and all(r["auth"] == "Bearer " + si.refreshed_token for r in uploads(si))


def test_a_token_that_has_not_expired_is_used_without_a_refresh(si, folder, tmp_path, sleeps):
    path = write_token_file(tmp_path, si, access=ACCESS, expiry="2099-01-01T00:00:00Z")
    c = drive.DriveClient(drive.OAuthTokenFile(path), si.url, sleep=sleeps)
    c.deliver(DECK, "report.pptx", folder)
    assert si.refreshes == 0


def test_a_token_that_expires_within_a_minute_is_renewed(si, folder, tmp_path, sleeps):
    path = write_token_file(tmp_path, si, access=ACCESS, expiry="2026-11-10T00:00:30Z")
    now = 1_794_268_800.0  # 2026-11-10T00:00:00Z
    source = drive.OAuthTokenFile(path, now=lambda: now)
    assert source() != ACCESS and si.refreshes == 1


def test_a_401_in_the_middle_renews_the_token_once_and_repeats_the_request(si, folder, tmp_path, sleeps):
    path = write_token_file(tmp_path, si, access=ACCESS, expiry="2099-01-01T00:00:00Z")
    c = drive.DriveClient(drive.OAuthTokenFile(path), si.url, sleep=sleeps)
    si.faults = ["401"]
    assert c.deliver(DECK, "report.pptx", folder).action == "created" and si.refreshes == 1


def test_a_refresh_the_server_refuses_is_an_error_that_names_no_secret(si, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"token": "x", "refresh_token": "wrong", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                               "token_uri": si.url + "/token", "expiry": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    with pytest.raises(drive.DriveError) as e:
        drive.OAuthTokenFile(bad)()
    assert e.value.reason == "tokenRefused" and "wrong" not in str(e.value)


def test_an_unreadable_token_file_is_a_config_error_without_the_path_or_contents(tmp_path):
    with pytest.raises(drive.DriveConfigError) as e:
        drive.OAuthTokenFile(tmp_path / "secret-folder-name" / "missing.json")
    assert "secret-folder-name" not in str(e.value)


def test_a_token_file_without_refresh_material_cannot_renew(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"token": "x", "expiry": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    with pytest.raises(drive.DriveConfigError, match="refresh"):
        drive.OAuthTokenFile(p)()


def test_the_token_file_is_never_written_back(si, tmp_path):
    path = write_token_file(tmp_path, si)
    before = path.read_bytes()
    drive.OAuthTokenFile(path)()
    assert path.read_bytes() == before


def test_no_secret_reaches_an_error_a_repr_or_a_log_line(si, folder, tmp_path, caplog, sleeps):
    secrets = [ACCESS, REFRESH, CLIENT_SECRET, si.refreshed_token]
    caplog.set_level(logging.DEBUG, logger="app.report.drive")
    source = drive.OAuthTokenFile(write_token_file(tmp_path, si))
    c = drive.DriveClient(source, si.url, sleep=sleeps)
    texts = [repr(c), repr(source), repr(drive.StaticToken(ACCESS))]
    c.deliver(DECK, "report.pptx", folder)
    for bad in ("nope",):
        with pytest.raises(drive.DriveError) as e:
            c.deliver(DECK, "report.pptx", bad)
        texts.append(str(e.value) + repr(e.value))
    si.faults = ["503"] * 10
    with pytest.raises(drive.DriveError) as e:
        c.deliver(DECK, "report.pptx", folder)
    texts += [str(e.value), repr(e.value), caplog.text]
    blob = "\n".join(texts)
    for secret in secrets:
        assert secret not in blob


def test_the_environment_names_the_missing_setting(tmp_path, si):
    with pytest.raises(drive.DriveConfigError, match="GOOGLE_DRIVE_FOLDER_ID"):
        drive.from_env({})
    with pytest.raises(drive.DriveConfigError, match="GOOGLE_DRIVE_TOKEN_FILE"):
        drive.from_env({"GOOGLE_DRIVE_FOLDER_ID": "f1"})
    client, folder_id = drive.from_env({"GOOGLE_DRIVE_FOLDER_ID": " f1 ", "GOOGLE_DRIVE_ACCESS_TOKEN": ACCESS, "GOOGLE_API_BASE": si.url})
    assert folder_id == "f1" and repr(client) == "DriveClient(base_url=%r)" % si.url
    client2, _ = drive.from_env({"GOOGLE_DRIVE_FOLDER_ID": "f1", "GOOGLE_DRIVE_TOKEN_FILE": str(write_token_file(tmp_path, si))})
    assert isinstance(client2._token, drive.OAuthTokenFile)


# ---------------------------------------------------------------- the report script's delivery step


@pytest.fixture()
def script():
    import monthly_report

    return monthly_report


def run_step(script, tmp_path, si, folder, content, extra=()):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(content)
    args = script.argparse.Namespace(month=(2026, 10), tenant="0b1c2d3e-aaaa-bbbb-cccc-1234567890ab", drive_folder=folder, drive_name=None)
    for k, v in extra:
        setattr(args, k, v)
    return script.deliver_to_drive(str(deck), args)


def test_the_script_delivers_the_deck_and_a_rerun_replaces_it(script, si, folder, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_DRIVE_ACCESS_TOKEN", ACCESS)
    monkeypatch.setenv("GOOGLE_API_BASE", si.url)
    monkeypatch.delenv("GOOGLE_DRIVE_TOKEN_FILE", raising=False)
    assert run_step(script, tmp_path, si, folder, DECK) == 0 and "created monthly-report-0b1c2d3e-2026-10.pptx" in capsys.readouterr().out
    assert run_step(script, tmp_path, si, folder, DECK) == 0 and "unchanged" in capsys.readouterr().out
    assert run_step(script, tmp_path, si, folder, DECK_B) == 0 and "replaced" in capsys.readouterr().out
    assert len(si.in_folder(folder)) == 1


def test_a_drive_failure_is_exit_3_with_the_reason_and_names_no_secret(script, si, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_DRIVE_ACCESS_TOKEN", ACCESS)
    monkeypatch.setenv("GOOGLE_API_BASE", si.url)
    monkeypatch.delenv("GOOGLE_DRIVE_TOKEN_FILE", raising=False)
    assert run_step(script, tmp_path, si, "no-such-folder", DECK) == 3
    err = capsys.readouterr().err
    assert "not delivered to Drive" in err and "not found" in err and ACCESS not in err


def test_a_missing_setting_is_exit_3_and_names_the_variable(script, si, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_DRIVE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_TOKEN_FILE", raising=False)
    assert run_step(script, tmp_path, si, "f1", DECK) == 3 and "GOOGLE_DRIVE_TOKEN_FILE" in capsys.readouterr().err


def test_the_script_documents_the_drive_options(script, capsys):
    with pytest.raises(SystemExit) as e:
        script.main(["--month", "2026-10", "--tenant", "t", "--help"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "--drive-folder" in out and "--drive-name" in out and "GOOGLE_DRIVE_FOLDER_ID" in out


# ---------------------------------------------------------------- property: random worlds


FAULT_POOL = ["429", "503", "500", "403rate", "drop_before", "drop_after", "POST:drop_after", "PATCH:drop_after", "GET:drop_before",
              "POST:503", "PATCH:503", "POST:429", "GET:503", "PUT:interrupt"]


@pytest.mark.parametrize("seed", range(40))
def test_whatever_goes_wrong_there_is_never_more_than_one_copy_and_a_success_holds_the_last_bytes(seed, si, sleeps):
    rng = random.Random(seed)
    c = drive.DriveClient(drive.StaticToken(ACCESS), si.url, sleep=sleeps)
    folder = si.add_folder()
    name = "report.pptx"
    if rng.random() < 0.3:
        si.add_file(name, folder, b"pre-existing")
    ids: set[str] = {f["id"] for f in si.in_folder(folder, name)}
    for step in range(rng.randint(2, 7)):
        content = rng.choice([DECK, DECK_B, b"third deck " * rng.randint(1, 50)])
        si.faults = [rng.choice(FAULT_POOL) for _ in range(rng.randint(0, 4))]
        upload = rng.choice(["auto", "auto", "resumable"])
        try:
            done = c.deliver(content, name, folder, upload=upload)
        except drive.DriveError:
            pass  # a step may run out of attempts; what must hold is below
        else:
            (stored,) = si.in_folder(folder, name)
            assert stored["content"] == content and done.file_id == stored["id"]
        live = si.in_folder(folder, name)
        assert len(live) <= max(1, len(ids)), "a copy was added"  # a pre-existing one counts; the run adds none
        ids |= {f["id"] for f in live}
        assert len(ids) <= 2  # the pre-existing file and at most the one this module created
        si.faults.clear()
