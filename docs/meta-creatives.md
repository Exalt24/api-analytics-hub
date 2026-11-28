# Meta ad creatives

A reader that lists every ad of a Meta ad account, or of the campaigns named, with what each ad says and where it goes: the ad id, name and status, the campaign, the ad copy (primary text, headline, link description, call to action), the destination links, the image, the video id with its source link, post link and length, a shareable preview link, and the page post link when the creative is built on a post. It was built for a request to "connect to the internal reporting and pull ad creative data such as links to ads (including video and ad copy) from multiple campaigns".

It was written from Meta's documentation (every page, its quote and what is UNVERIFIED are in [META_SOURCES.md](META_SOURCES.md#ad-creatives)) and tested against a stand-in server written from the same pages. No Meta ad account is held and none was used, so it has never made a call to Meta. What the tests prove and do not prove is at the end.

## Run it

```bash
# the local run: starts the stand-in server with a small sample account, needs no token
python scripts/meta_creatives.py --stand-in --out-dir out

# a real account (credentials from the environment only)
META_AD_ACCOUNT_ID=<number> META_ACCESS_TOKEN=<token> \
    python scripts/meta_creatives.py --out-dir out --campaign 123,456 --status ACTIVE,PAUSED
```

It writes `out/meta_creatives_<account>.csv` and `out/meta_creatives_<account>.json`. `--campaign` takes campaign ids (repeatable or comma separated; default every ad of the account), `--status` the `effective_status` values to read (default every status but DELETED), `--include-deleted` adds DELETED, and `META_API_BASE_URL` points the run at another server. [A saved run](test-runs/2026-11-28-meta-creatives-run.txt) shows the stand-in run, the CSV and the start of the JSON file.

Exit 0: finished. Exit 1: it ran and failed, and nothing is written (an expired token, an account it may not read, an answer in a shape the pages do not describe). Exit 2: it could not start (a malformed account id, a campaign id that is not a number, a status Meta does not list, a missing token). Exit 3: a rate limit stopped it; both files hold the ads read so far and say `"complete": false` with the reason.

## What a row holds

One CSV row per ad. A cell with many values holds them joined by ` | `; the JSON file keeps lists as lists and also holds the carousel cards. A CSV cell that would start a spreadsheet formula (`=`, `+`, `-`, `@`) gets a leading apostrophe, because ad copy is somebody else's text; the JSON file is not changed.

| Column | Meaning |
|---|---|
| `ad_id`, `ad_name`, `ad_status`, `ad_effective_status`, `adset_id` | the ad |
| `campaign_id`, `campaign_name` | its campaign |
| `creative_id`, `creative_kind`, `creative_status` | the creative and its shape: `link`, `carousel`, `video`, `photo`, `dynamic` (an asset feed), `post` (an existing page post) or `unknown` |
| `primary_text`, `headline`, `link_description`, `call_to_action_type` | the copy, from the place each shape keeps it |
| `destination_links` | every address the ad can send a person to, once each |
| `image_urls`, `image_hashes`, `thumbnail_url` | the images; a hash is shown when the creative names an image only by hash |
| `video_ids`, `video_source_urls`, `video_permalinks`, `video_lengths` | one entry per video, in the same order |
| `ad_preview_link` | the ad's `preview_shareable_link`, as Meta sends it |
| `post_id`, `post_permalink` | `effective_object_story_id` and a link built from it |
| `instagram_permalink_url` | for an ad on an Instagram post |
| `notes` | what is missing and why: a video with no source, a video nobody may read, a deleted creative, a creative that came back empty, an ad on a post whose copy is not on the creative |

## How it reads the shapes

| Shape | Where the copy, links and media are |
|---|---|
| link | `object_story_spec.link_data`: `message`, `name`, `description`, `link`, `call_to_action.value.link`, `picture` |
| carousel | `link_data` plus `child_attachments`, each card with `name`, `description`, `link`, `picture` or `image_hash`, `video_id` |
| video | `object_story_spec.video_data`: `message`, `title`, `link_description`, `video_id`, `image_url`, `call_to_action` |
| photo | `object_story_spec.photo_data`: `caption`, `url` |
| dynamic | `asset_feed_spec`: `bodies`, `titles`, `descriptions`, `link_urls`, `videos`, `images`, `call_to_action_types` (an object, or a JSON string as the Ad Creative page describes the field) |
| existing post | `object_story_id` and `effective_object_story_id`; no copy is on the creative, and the row says so |
| no spec | the creative's own `body`, `title`, `call_to_action_type`, `object_url`, `video_id`, `image_url` |

A value that is empty or null is left out, and a value that two places give (the link and the button's link are required to match) appears once. A field of the wrong type stops the run as a broken answer instead of becoming empty copy.

## How it talks to Meta

- **The edges.** `GET /act_{id}/ads`, or `GET /{campaign}/ads` once per campaign, with `fields` asking for the ad, `campaign{id,name}` and `creative{...}`, `limit`, `after` and the `effective_status` filter.
- **Paging.** A `next` link is never requested, because it carries the access token; the next page is built from the same base address and the `after` cursor. A page that is empty but has a `next` is followed, a cursor that comes back is refused, and an ad that appears on two pages is kept once.
- **Videos.** The ids found on a page of ads are looked up in a batch (`POST` with a `batch` list, at most 50 requests each), once per id for the whole run, with the token in the form body and nothing in the URL. `source` is on the Video page; `permalink_url` and `length` are asked too and are UNVERIFIED there. A video with no source, one the token may not read, or one that never answers becomes a note on the ad, not a failed run.
- **Expansion.** The Field Expansion page says most of the Marketing API cannot expand some connections, so a creative that comes back as an id alone, or a campaign without a name, is read afterwards by id in a batch.
- **Pacing.** A throttle or temporary error code is retried with a bounded wait (the usage header's minutes, else `Retry-After`, else a jittered backoff); a wait over 30 seconds is handed back as a rate limit. Before a further page the reader stops if any usage percentage in the headers is 90 or more, and keeps the pages already read.
- **Errors** are classified by the error object's `code`, whatever the HTTP status, as in [`meta.py`](../app/connectors/meta.py): an expired token and a permission error stop the run at once, the Marketing API throttle codes (including 80004) are retried, anything else is a broken answer.

## What it does not do

It does not call Meta (no account is held), does not get or refresh a token, does not read insights or spend (that is `meta.py`), and does not use the Ad Previews edge, whose page says it returns an iframe valid for 24 hours. It cannot say what a live account's creatives look like, which fields Meta leaves out when they are empty, whether the token of an ad account may read a video's `source`, whether the `source` address expires, or what the preview link is. The page post link is built from the post id's two parts and is UNVERIFIED as an address.

## Tests

- `tests/test_meta_creatives.py`: every creative shape, the request, paging, the error codes, pacing, the batch (a hundred and twenty videos in three requests), videos with no source or no access, expansion that falls back to a read by id.
- `tests/test_meta_creatives_oracle.py`: 80 seeded random accounts, each built together with its expected answer (so the check does not run the reader's own code), read back through the stand-in, with transient faults on random requests, batches that time out, and campaign and status filters.
- `tests/test_meta_creatives_fake_server.py`: the stand-in refuses what the pages say Meta refuses (an unknown field at any depth, a status the Ad page does not list, a batch of 51, an invalid cursor) and answers what they describe, with a round trip of the field list parser.
- `tests/test_meta_creatives_cli.py`: the CSV and JSON files, the exit codes, the formula guard and the partial run.
- `tests/mutation_check_creatives.py`: guards broken one at a time in a temporary copy of the code, each of which must turn a named test red, after a control that runs the unmutated copy first. Its result is saved in [the mutation run](test-runs/2026-11-28-meta-creatives-mutation.txt) and the whole suite in [the pytest run](test-runs/2026-11-28-meta-creatives-pytest.txt).

What these prove: the reader sends the requests the pages describe, reads the shapes the pages describe, and survives the faults the tests make. What they do not prove: that Meta sends those shapes or accepts those requests. That needs a live ad account, and none is held.
