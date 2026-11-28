# LIFESPAN: permanent (the repo's own mutation check for the Meta ad creative reader, its stand-in server and its CLI, run by hand)
"""Mutation control for app/connectors/meta_creatives.py, meta_creatives_fake.py and scripts/meta_creatives.py.

Break each guard one at a time and prove the tests for it notice. It follows tests/mutation_check_gbp.py and demands more
than "the suite went red": each mutant names the tests of which at least one must be among the failures, so a guard cannot
be "covered" by an unrelated test that happens to break.

It works on a COPY of the repository (app, scripts/meta_creatives.py and the creative test files), made in a temporary
folder per worker and deleted at the end, so a killed run leaves nothing weakened in the real tree. It needs no database and
no Docker. THE CONTROL COMES FIRST: the unmutated copy must pass every test file, or nothing is compared. A mutant whose
text is not found exactly once is an error, not a pass, so a refactor that moves a guard cannot quietly turn this into a
check of nothing.

    python tests/mutation_check_creatives.py --check-targets       only confirm every guard finds its text, run nothing
    python tests/mutation_check_creatives.py --match page,batch    only the guards whose name holds a word
    python tests/mutation_check_creatives.py --jobs 4              workers (default 3, each with its own copy)
    python tests/mutation_check_creatives.py                       every guard
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "app/connectors/meta_creatives.py"
FAKE = "app/connectors/meta_creatives_fake.py"
CLI = "scripts/meta_creatives.py"
T_CLIENT = "tests/test_meta_creatives.py"
T_ORACLE = "tests/test_meta_creatives_oracle.py"
T_FAKE = "tests/test_meta_creatives_fake_server.py"
T_CLI = "tests/test_meta_creatives_cli.py"
ALL_TESTS = (T_CLIENT, T_ORACLE, T_FAKE, T_CLI, "tests/meta_creatives_world.py")
BASELINE_TIMEOUT_SECONDS = 300
MUTANT_TIMEOUT_SECONDS = 150
#: Seeded worlds the oracle reads for each mutant; the control reads all 80.
MUTANT_SEEDS = 24


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()


def M(name, target, old, new, files, expected, why):
    return (name, target, old, new, files, expected, why)


# (name, target file, old text, new text, test files to run, test-name fragments of which at least one must fail, why it matters)
MUTATIONS = [
    # -- the request
    M("fields: the creative expansion is not asked for", CLIENT,
      '"campaign{id,name},creative{%s}" % ",".join(CREATIVE_FIELDS)', '"campaign{id,name}"',
      [T_CLIENT], ["asked_for_the_creative_expansion", "every_documented_creative_shape"], "without it there is no copy to read"),
    M("fields: the status filter is not sent", CLIENT,
      ',\n                      "effective_status": json.dumps(list(statuses), separators=(",", ":"))}', "}",
      [T_CLIENT], ["asked_for_the_creative_expansion", "deleted_ads_are_read_only"], "deleted ads would come back in whatever Meta's default is"),
    M("status: deleted ads are read by default", CLIENT,
      'DEFAULT_STATUSES = tuple(s for s in AD_STATUSES if s != "DELETED")', "DEFAULT_STATUSES = tuple(AD_STATUSES)",
      [T_CLIENT, T_ORACLE], ["deleted_ads_are_read_only", "asked_for_the_creative_expansion", "every_world"], "a deleted ad is only read on request"),
    M("status: include_deleted is ignored", CLIENT,
      'if include_deleted and "DELETED" not in chosen:', "if False:",
      [T_CLIENT], ["deleted_ads_are_read_only"], "the caller asked for deleted ads"),
    M("status: a value the Ad page does not list is accepted", CLIENT,
      "if bad or not chosen:", "if False:",
      [T_CLIENT], ["status_the_ad_page_does_not_list"], "a typo would come back as an error from Meta after the run started"),
    M("paging: the page size is not sent", CLIENT,
      '"limit": str(self._page_size),', '"limit": "25",',
      [T_CLIENT, T_ORACLE], ["asked_for_the_creative_expansion", "every_world"], "the limit is how a page is sized"),
    M("campaign: the filter reads the whole account", CLIENT,
      'edges = ["%s/ads" % c for c in dict.fromkeys(ids)]', 'edges = ["act_%s/ads" % self.account_id]',
      [T_CLIENT, T_ORACLE], ["campaign_ids_must_be_numbers", "campaign_filter"], "ads of other campaigns would be reported"),
    M("campaign: a repeated id is read twice", CLIENT,
      'edges = ["%s/ads" % c for c in dict.fromkeys(ids)]', 'edges = ["%s/ads" % c for c in ids]',
      [T_CLIENT], ["each_is_read_once"], "a duplicate would spend calls and be skipped as duplicate ads"),
    M("campaign: an id that is not a number is accepted", CLIENT,
      "if bad or not ids:", "if False:",
      [T_CLIENT], ["campaign_ids_must_be_numbers"], "an id with a slash would change the path that is called"),
    M("account: the id rule is not applied", CLIENT,
      "_ACCOUNT_RE.match(str(ad_account_id).strip())", 're.match(r"^(?:act_)?(.*)$", str(ad_account_id).strip())',
      [T_CLIENT], ["malformed_ad_account_id"], "a wrong id comes back as a permissions error that reads like a bad token"),
    # -- paging
    M("paging: the next link is followed", CLIENT,
      ["        after: str | None = None\n        seen_cursors: set[str] = set()",
       '            body = (await self._send("GET", path, params=params))["body"]',
       "            after = following\n        raise UpstreamBroken(\"more than"],
      ["        after: str | None = None\n        seen_cursors: set[str] = set()\n        follow = None",
       '            body = (await self._send("GET", path, params=params))["body"] if not follow else (await self._client.get(follow)).json()',
       "            after = following\n            follow = paging.get(\"next\")\n        raise UpstreamBroken(\"more than"],
      [T_CLIENT], ["next_link_is_never_requested"], "a next link carries the token to whatever address the response names"),
    M("paging: an empty page ends the run", CLIENT,
      "            if not has_next:\n                return", "            if not has_next or not rows:\n                return",
      [T_CLIENT, T_ORACLE], ["empty_page_that_still_has_a_next", "every_world"], "the paging page allows an empty page with a next link"),
    M("paging: a cursor that comes back is accepted", CLIENT,
      "if following == after or following in seen_cursors:", "if False:",
      [T_CLIENT], ["cursor_that_comes_back"], "a response could loop the reader for ever"),
    M("paging: a next link with no cursor is accepted", CLIENT,
      "if not isinstance(following, str) or not following:", "if False:",
      [T_CLIENT], ["no_after_cursor"], "the next request cannot be built without a cursor"),
    M("paging: there is no page limit", CLIENT,
      "for _ in range(self._max_pages):", "for _ in range(10 ** 9):",
      [T_CLIENT], ["never_end_stop_at_the_page_limit"], "cursors that keep changing would never end"),
    M("paging: the same ad twice is kept twice", CLIENT,
      "if ad_id in seen:", "if False:",
      [T_CLIENT], ["same_ad_on_two_pages"], "a sliding cursor repeats an ad"),
    M("paging: a row outside the status filter is kept", CLIENT,
      "if effective is not None and effective not in statuses:", "if False:",
      [T_CLIENT], ["outside_the_status_filter"], "the filter is asked for and checked"),
    # -- errors
    M("errors: an expired token is retried", CLIENT,
      '        code = err.code\n        if code in _EXPIRED_CODES or (code is None and err.http == 401):\n            raise AuthExpired("the access token was rejected (%s)" % err.text())\n        if code in _PERMISSION_CODES or (code is None and err.http == 403):',
      '        code = err.code\n        if False:\n            raise AuthExpired("x")\n        if code in _PERMISSION_CODES or (code is None and err.http == 403):',
      [T_CLIENT], ["retrying_cannot_fix", "documented_shape_is_handled"], "retrying an expired token only spends calls"),
    M("errors: a 401 with no error object is not an expired token", CLIENT,
      '        if code in _EXPIRED_CODES or (code is None and err.http == 401):\n            raise AuthExpired("the access token was rejected (%s)" % err.text())\n        if code in _PERMISSION_CODES or (code is None and err.http == 403):',
      '        if code in _EXPIRED_CODES:\n            raise AuthExpired("the access token was rejected (%s)" % err.text())\n        if code in _PERMISSION_CODES or (code is None and err.http == 403):',
      [T_CLIENT], ["documented_shape_is_handled"], "a 401 with an empty body is still a rejected token"),
    M("errors: a 403 with no error object is not no-access", CLIENT,
      "if code in _PERMISSION_CODES or (code is None and err.http == 403):\n            raise AuthRevoked", "if code in _PERMISSION_CODES:\n            raise AuthRevoked",
      [T_CLIENT], ["documented_shape_is_handled"], "a 403 with an empty body is still no access"),
    M("errors: a permission error is retried", CLIENT,
      "if code in _PERMISSION_CODES or (code is None and err.http == 403):\n            raise AuthRevoked", "if False:\n            raise AuthRevoked",
      [T_CLIENT], ["retrying_cannot_fix", "documented_shape_is_handled"], "a person must fix the access"),
    M("errors: a refused request is retried", CLIENT,
      'raise UpstreamBroken("Meta refused the request (%s)" % err.text())', "return",
      [T_CLIENT], ["retrying_cannot_fix", "campaign_that_does_not_exist"], "a bad parameter will not pass by waiting"),
    M("errors: the Marketing API throttle codes are not known", CLIENT,
      "_THROTTLE = _THROTTLE_CODES | frozenset({80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014})", "_THROTTLE = _THROTTLE_CODES",
      [T_CLIENT], ["throttle_or_temporary_code"], "80004 is the ad account throttle on the ads edge page"),
    M("errors: an error object inside a 200 is data", CLIENT,
      "                err = _read_error(body, response.status_code)\n                if err is None and response.status_code == 200:",
      "                err = None if response.status_code == 200 else _read_error(body, response.status_code)\n                if err is None and response.status_code == 200:",
      [T_CLIENT], ["error_object_inside_a_200"], "Graph can answer 200 with an error object"),
    M("errors: the attempt limit is not enforced", CLIENT,
      "last = attempt == self._max_attempts - 1", "last = False",
      [T_CLIENT], ["throttle_or_temporary_code", "documented_shape_is_handled", "no_error_message_holds"], "retries must be bounded"),
    # -- pacing
    M("pacing: a long wait is slept instead of handed back", CLIENT,
      "            if delay > self._backoff_cap:\n                raise RateLimited(wait, \"asked to wait", "            if False:\n                raise RateLimited(wait, \"asked to wait",
      [T_CLIENT], ["long_wait_is_handed_back"], "Meta's header gives minutes"),
    M("pacing: Retry-After is ignored", CLIENT,
      'raw = response.headers.get("Retry-After")', "raw = None",
      [T_CLIENT], ["retry_after_and_the_usage_header"], "a numeric Retry-After is the wait when the usage header names none"),
    M("pacing: the regain time is read as seconds", CLIENT,
      "return regain_minutes * 60", "return regain_minutes",
      [T_CLIENT], ["retry_after_and_the_usage_header"], "estimated_time_to_regain_access is in minutes"),
    M("pacing: the second spelling of the business use case header is ignored", CLIENT,
      'raw = response.headers.get("x-business-use-case")', "raw = None",
      [T_CLIENT], ["read_under_both_names"], "the two rate limit pages name the header differently"),
    M("pacing: usage near the limit does not stop the next page", CLIENT,
      "            if after and self.utilisation is not None and self.utilisation >= self._ceiling:", "            if False:",
      [T_CLIENT], ["not_fetched_when_usage_is_near"], "the page says to stop when you come close to 100 percent"),
    M("pacing: usage is not checked between campaigns", CLIENT,
      "            if n and self.utilisation is not None and self.utilisation >= self._ceiling:", "            if False:",
      [T_CLIENT], ["checked_between_campaigns"], "a second campaign is a further call"),
    M("pacing: the pages read are lost on a rate limit", CLIENT,
      "        except RateLimited as exc:\n            return CreativeRun", "        except ZeroDivisionError as exc:\n            return CreativeRun",
      [T_CLIENT], ["not_fetched_when_usage_is_near", "checked_between_campaigns"], "the pages already read are kept"),
    M("batch: the token goes in the URL", CLIENT,
      'response = await self._client.post(url, data={**(form or {}), "access_token": token})',
      'response = await self._client.post(url, params={"access_token": token}, data=form)',
      [T_CLIENT, T_ORACLE], ["token_in_the_body", "batches_are_small"], "a URL is logged"),
    # -- the batch of lookups
    M("batch: fifty requests become a hundred", CLIENT,
      "BATCH_LIMIT = 50", "BATCH_LIMIT = 100",
      [T_CLIENT, T_ORACLE], ["three_batches", "every_world", "batches_are_small"], "the Batch Requests page limits a batch to 50"),
    M("batch: a video seen on an earlier page is asked again", CLIENT,
      "todo = [v for v in dict.fromkeys(wanted) if v not in videos]", "todo = [v for v in dict.fromkeys(wanted) if True]",
      [T_CLIENT], ["looked_up_once_across_pages"], "each lookup counts as a call against the limit"),
    M("batch: a request that timed out is not asked again", CLIENT,
      '        if item is None:\n            return "retry", None', '        if item is None:\n            return "done", _Looked(problem="x")',
      [T_CLIENT, T_ORACLE], ["timed_out_or_was_throttled", "every_world"], "a null answer is a request to retry"),
    M("batch: a throttled answer is not asked again", CLIENT,
      '            return "retry", err', '            return "done", _Looked(problem="throttled")',
      [T_CLIENT, T_ORACLE], ["timed_out_or_was_throttled", "every_world"], "a throttle passes"),
    M("batch: every id is asked again, not only the failed", CLIENT,
      "            pending = retry", "            pending = pending",
      [T_CLIENT], ["timed_out_or_was_throttled"], "the retry spends calls only on what failed"),
    M("batch: a wrong number of answers is accepted", CLIENT,
      "if not isinstance(body, list) or len(body) != len(chunk):", "if not isinstance(body, list):",
      [T_CLIENT], ["wrong_number_of_results"], "an answer cannot be matched to its request"),
    M("batch: a permission problem on one id stops the run", CLIENT,
      'return "done", _Looked(problem="no access (%s)" % err.text())', 'raise AuthRevoked("x")',
      [T_CLIENT], ["no_source_a_null_source_no_access", "expired_token_inside_a_batch"], "one video nobody may read must not lose the other ads"),
    M("batch: an expired token inside the batch is a note", CLIENT,
      '        code = err.code\n        if code in _EXPIRED_CODES or (code is None and err.http == 401):\n            raise AuthExpired("the access token was rejected (%s)" % err.text())\n        if code in _THROTTLE or code in _TEMPORARY_CODES or (code is None and (err.http == 429',
      '        code = err.code\n        if False:\n            raise AuthExpired("x")\n        if code in _THROTTLE or code in _TEMPORARY_CODES or (code is None and (err.http == 429',
      [T_CLIENT], ["expired_token_inside_a_batch"], "every later call would fail the same way"),
    M("batch: a non numeric id is put in a url", CLIENT,
      '_ID_RE = re.compile(r"^\\d{1,25}$")', '_ID_RE = re.compile(r"^.{1,25}$")',
      [T_CLIENT], ["not_a_number_is_not_put_in_a_url", "campaign_ids_must_be_numbers"], "an id from a response could add parameters to a request"),
    M("batch: the video fields are not asked for", CLIENT,
      'VIDEO_FIELDS = "source,permalink_url,length"', 'VIDEO_FIELDS = "permalink_url,length"',
      [T_CLIENT, T_ORACLE, T_CLI], ["every_documented_creative_shape", "every_world", "stand_in_run"], "the source is the point of the video lookup"),
    M("batch: the lookup url drops the fields", CLIENT,
      '"relative_url": "%s?fields=%s" % (i, fields)', '"relative_url": "%s" % i',
      [T_CLIENT], ["every_documented_creative_shape"], "a node read with no fields answers an id only"),
    # -- reading a video
    M("video: a missing source is not noted", CLIENT,
      'problems.append("no source returned")', "pass",
      [T_CLIENT, T_ORACLE], ["no_source_a_null_source", "every_world"], "a video with no source must say so"),
    M("video: a relative permalink is kept relative", CLIENT,
      'permalink = FACEBOOK + permalink if permalink.startswith("/") else permalink', "permalink = permalink",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a relative path is not a link"),
    M("video: a length that is not a number is kept", CLIENT,
      "if isinstance(length, bool) or not isinstance(length, (int, float)) or length < 0:", "if False:",
      [T_CLIENT], ["length_that_is_not_a_number"], "a length of true or minus one is a broken answer"),
    # -- the creative shapes
    M("shape: carousel cards are not read", CLIENT,
      'for i, child in enumerate(_list(data.get("child_attachments"), "link_data.child_attachments")):', "for i, child in enumerate([]):",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a carousel's copy and links are on its cards"),
    M("shape: dynamic creative bodies are not read", CLIENT,
      'parts.primary += _asset_texts(spec.get("bodies"), "text", "asset_feed_spec.bodies")', "pass",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a dynamic creative's copy is in asset_feed_spec"),
    M("shape: dynamic creative videos are not read", CLIENT,
      'parts.videos += _asset_texts(spec.get("videos"), "video_id", "asset_feed_spec.videos")', "pass",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "its videos are in asset_feed_spec.videos"),
    M("shape: an asset feed sent as a string is not parsed", CLIENT,
      "    if isinstance(feed, str):", "    if False:",
      [T_CLIENT, T_ORACLE], ["json_string", "every_world"], "the Ad Creative page describes the field as a JSON string"),
    M("shape: video_data is not read", CLIENT,
      "    if video_data:\n        _read_video_data(video_data, parts)", "    if False:\n        _read_video_data(video_data, parts)",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a video ad's copy is in video_data"),
    M("shape: photo_data is not read", CLIENT,
      "    if photo_data:\n        _read_photo_data(photo_data, parts)", "    if False:\n        _read_photo_data(photo_data, parts)",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a photo ad's caption is in photo_data"),
    M("shape: the call to action link is not read", CLIENT,
      'parts.links.append(_text(_object(cta.get("value"), what + ".value").get("link"), what + ".value.link"))', "pass",
      [T_CLIENT], ["call_to_action_link_that_differs"], "the button can point somewhere the link field does not"),
    M("shape: the creative's own body is not used", CLIENT,
      '        parts.primary.append(_text(creative.get("body"), "body"))', "        pass",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "an ad built without a spec carries its copy on the creative"),
    M("shape: the creative's own video id is not used", CLIENT,
      '    parts.videos.append(_text(creative.get("video_id"), "video_id"))', "    pass",
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "a video ad without a spec names its video on the creative"),
    M("shape: an existing post is not named as one", CLIENT,
      '        parts.kind = "post"', '        parts.kind = "unknown"',
      [T_CLIENT, T_ORACLE], ["every_documented_creative_shape", "every_world"], "its copy is not on the creative and the report must say so"),
    M("shape: a post link is built from any id", CLIENT,
      '_STORY_RE = re.compile(r"^(\\d{1,25})_(\\d{1,25})$")', '_STORY_RE = re.compile(r"^(\\d+)_(\\d+).*$")',
      [T_CLIENT], ["story_id_that_is_not_page_underscore_post"], "the pages give the id's form only as Post ID"),
    M("shape: a wrong type is read as no copy", CLIENT,
      '        raise UpstreamBroken("%s is %s, not a string" % (what, type(value).__name__))', "        return None",
      [T_CLIENT], ["wrong_type_is_a_broken_answer"], "a shape change must surface, not chart nothing"),
    M("shape: a value is listed twice", CLIENT,
      "    seen: dict[str, None] = {}\n    for v in values:\n        if v:\n            seen.setdefault(v, None)\n    return list(seen)", "    return [v for v in values if v]",
      [T_CLIENT, T_ORACLE], ["each_value_appears_once", "every_world", "first_seen_order"], "the link and the button link are the same address"),
    M("shape: an empty string is kept as copy", CLIENT,
      "    for v in values:\n        if v:\n            seen.setdefault(v, None)", "    for v in values:\n        if v is not None:\n            seen.setdefault(v, None)",
      [T_CLIENT], ["empty_and_null_copy"], "an empty field is no copy"),
    # -- what the response does not expand
    M("fallback: a creative that came back as an id is not read by id", CLIENT,
      'if isinstance(a.get("creative"), dict) and set(a["creative"]) <= {"id"}', 'if False and isinstance(a.get("creative"), dict)',
      [T_CLIENT, T_ORACLE], ["expansion_the_server_refuses", "every_world"], "the Field Expansion page says most of the Marketing API cannot expand some connections"),
    M("fallback: a campaign with no name is not read by id", CLIENT,
      "need_campaign = [c for c in _unique(self._campaign_of(a)[0] for a in ads) if c not in names and _ID_RE.match(c)]", "need_campaign = []",
      [T_CLIENT, T_ORACLE], ["expansion_the_server_refuses", "no_creative_object", "every_world"], "the report names the campaign"),
    # -- the stand-in server
    M("stand-in: a batch of fifty one is answered", FAKE,
      "if len(items) > BATCH_LIMIT:", "if len(items) > BATCH_LIMIT + 1:",
      [T_FAKE], ["fifty_is_answered"], "the Batch Requests page limits a batch to 50"),
    M("stand-in: an unknown field is answered", FAKE,
      "if known is not None and name not in known:", "if False:",
      [T_FAKE], ["unknown_field_is_refused"], "a field the page does not list must be refused"),
    M("stand-in: the status filter is ignored", FAKE,
      'rows = [a for a in ads if a.get("effective_status", a.get("status")) in wanted]', "rows = list(ads)",
      [T_FAKE, T_CLIENT], ["deleted_ad_comes_back_only", "deleted_ads_are_read_only"], "deleted ads must need the filter"),
    M("stand-in: a batch is answered without the token", FAKE,
      '            if params.get("access_token") != self.token:\n                return self._err(190, "OAuthException", "Invalid OAuth access token.")\n            return self._batch(params)',
      "            return self._batch(params)",
      [T_FAKE], ["batch_needs_the_token"], "a stand-in that accepts anything lets the client skip the token"),
    M("stand-in: a next link carries no token", FAKE,
      'paging["next"] = "%s/v26.0/%s?after=%s&access_token=%s" % (base, edge, paging["cursors"]["after"], self.token)',
      'paging["next"] = "%s/v26.0/%s?after=%s" % (base, edge, paging["cursors"]["after"])',
      [T_FAKE], ["limit_and_cursor_rules"], "the stand-in must be as dangerous as the pages say a next link is"),
    M("stand-in: a cursor it did not issue is accepted", FAKE,
      'if "after" in params and _decode(params["after"]) is None:', "if False:",
      [T_FAKE], ["limit_and_cursor_rules"], "Meta refuses an invalid cursor"),
    M("stand-in: the field list parser accepts a brace that is never closed", FAKE,
      '            raise ValueError("a { that is never closed")', "            pass",
      [T_FAKE], ["query_that_does_not_parse", "round_trips"], "a malformed fields value is error 2500"),
    # -- the command line
    M("cli: a formula is written as it is", CLI,
      "return \"'\" + value if value.startswith(_FORMULA_START) else value", "return value",
      [T_CLI], ["start_a_spreadsheet_formula"], "ad copy is somebody else's text and a spreadsheet runs formulas"),
    M("cli: a partial run exits 0", CLI,
      "            return 3", "            return 0",
      [T_CLI], ["rate_limit_exits_3"], "a partial file must not look like a finished one"),
    M("cli: the json file always says complete", CLI,
      '"complete": run.complete,', '"complete": True,',
      [T_CLI], ["rate_limit_exits_3"], "the file itself must say it is partial"),
    M("cli: a failed run writes files anyway", CLI,
      '            print("the run failed (%s): %s" % (exc.code, exc), file=sys.stderr)\n            return 1',
      '            args.out_dir.mkdir(parents=True, exist_ok=True)\n            print("the run failed (%s): %s" % (exc.code, exc), file=sys.stderr)\n            return 1',
      [T_CLI], ["refused_token_exits_1"], "nothing is written for a run that failed"),
]


def _texts(entry):
    old, new = entry[2], entry[3]
    return (old if isinstance(old, list) else [old]), (new if isinstance(new, list) else [new])


def missing_targets(chosen) -> list[str]:
    """Guards whose old text is gone from its file, appears more than once, or changes nothing. Such a guard proves nothing."""
    cache: dict[str, str] = {}
    problems = []
    for entry in chosen:
        name, target = entry[0], entry[1]
        text = cache.setdefault(target, (ROOT / target).read_text(encoding="utf-8").replace("\r\n", "\n"))
        olds, news = _texts(entry)
        for o, n in zip(olds, news):
            if text.count(o) != 1:
                problems.append("%s: its target text appears %d times in %s, it must appear once" % (name, text.count(o), target))
            elif o == n:
                problems.append("%s: the mutation changes nothing" % name)
    return problems


def make_copy() -> Path:
    copy = Path(tempfile.mkdtemp(prefix="creativesmut-"))
    shutil.copytree(ROOT / "app", copy / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (copy / "scripts").mkdir()
    shutil.copyfile(ROOT / CLI, copy / CLI)
    (copy / "tests").mkdir()
    for rel in ALL_TESTS:
        shutil.copyfile(ROOT / rel, copy / rel)
    return copy


def run_suite(copy: Path, files, timeout: int, seeds: int = 80):
    """Returns (all_green, failed test names), or (None, []) when the run hung."""
    for stale in (copy / "app" / "connectors" / "__pycache__").glob("*.pyc"):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CREATIVES_ORACLE_SEEDS": str(seeds)}
    try:
        r = subprocess.run([str(PY), "-B", "-m", "pytest", *files, "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
                           cwd=copy, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, []
    failed = [line.split(" ", 1)[1] for line in r.stdout.splitlines() if line.startswith("FAILED ")]
    return r.returncode == 0, failed


def judge(copy: Path, entry) -> tuple[str, str]:
    name, target, _, _, files, expected, why = entry
    path = copy / target
    original = path.read_bytes()
    text = original.decode("utf-8").replace("\r\n", "\n")
    olds, news = _texts(entry)
    mutated = text
    for o, n in zip(olds, news):
        mutated = mutated.replace(o, n, 1)
    assert mutated != text, "%s: the mutation changed nothing, so a green result would mean nothing" % name
    try:
        path.write_bytes(mutated.encode("utf-8"))
        green, failed = run_suite(copy, files, MUTANT_TIMEOUT_SECONDS, MUTANT_SEEDS)
    finally:
        path.write_bytes(original)
    if green is None:
        return "ok", "%s: suite HUNG, so the guard also stops an endless loop" % name
    if green:
        return "FAIL", "%s: suite stayed GREEN, so nothing tests that %s" % (name, why)
    if any(frag in test for test in failed for frag in expected):
        return "ok", "%s: failed %d test(s), including the expected one" % (name, len(failed))
    return "FAIL", "%s: the suite went red but not in %s; failed: %s" % (name, expected, failed[:4])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--check-targets", action="store_true", help="only check that every guard finds its target text, edit nothing")
    parser.add_argument("--match", help="comma separated words; only guards whose name holds one run")
    parser.add_argument("--jobs", type=int, default=3, help="workers, each with its own copy of the repository")
    parser.add_argument("--no-control", action="store_true", help="skip the unmutated control (a chunk of a longer run only)")
    args = parser.parse_args(argv)
    words = args.match.split(",") if args.match else None
    chosen = [m for m in MUTATIONS if not words or any(w in m[0] for w in words)]
    problems = missing_targets(chosen)
    if problems:
        print("update this file before mutating:\n  " + "\n  ".join(problems))
        return 1
    if args.check_targets:
        print("all %d guards find their target" % len(chosen))
        return 0

    copies = [make_copy() for _ in range(max(1, args.jobs))]
    started = time.monotonic()
    try:
        if not args.no_control:
            green, failed = run_suite(copies[0], [t for t in ALL_TESTS if t.endswith(".py") and "test_" in t], BASELINE_TIMEOUT_SECONDS)
            if green is not True:
                print("CONTROL: the unmutated code does not pass, nothing to compare against; failed: %s" % failed[:5])
                return 1
            print("control: the unmutated code passes every test file")
        queue = list(chosen)
        results: list[tuple[str, str]] = []

        def worker(copy: Path):
            out = []
            while True:
                try:
                    entry = queue.pop(0)
                except IndexError:
                    return out
                status, line = judge(copy, entry)
                print("[%s] %s" % (status.ljust(4), line), flush=True)
                out.append((status, line))

        with futures.ThreadPoolExecutor(max_workers=len(copies)) as pool:
            for out in pool.map(worker, copies):
                results += out
        bad = [line for status, line in results if status != "ok"]
        print()
        print("mutants run %d: killed %d, not caught %d (%.0f s)" % (len(results), len(results) - len(bad), len(bad), time.monotonic() - started))
        print("mutation control: %s" % ("PASS" if not bad else "%d UNTESTED GUARD(S)" % len(bad)))
        return 1 if bad else 0
    finally:
        for copy in copies:
            shutil.rmtree(copy, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
