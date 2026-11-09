---
type: reference
updated: 2026-11-10
---
*The monthly deck delivered to a Google Drive folder through the Drive API v3, written from Google's documentation, tested against a stand-in server and run once for real.*

## What it does

`app/report/drive.py` (standard library only) is a delivery step for the deck the monthly report writes. `scripts/monthly_report.py --drive-folder <folder id>` (or `GOOGLE_DRIVE_FOLDER_ID`) runs it after the deck is built:

1. Reads the folder and checks it is a folder, not in the trash. A wrong id is one clear error and nothing is uploaded.
2. Searches the folder for the file name: `name = '...' and 'FOLDER' in parents and trashed = false`, oldest first, with an apostrophe and a backslash in the name escaped as the search guide says.
3. No match: creates the file. A match with a different `md5Checksum`: replaces its content with a PATCH, same file id. A match with the same checksum: sends nothing and says `unchanged`.
4. A second file with the same name that someone made by hand is reported (`duplicates`) and left alone. The module never deletes.

The file name is `monthly-report-<first 8 characters of the tenant id>-<month>.pptx`, the same for the same tenant and month (`--drive-name` overrides it). That is what makes a rerun a replace and never a second copy.

## From Google's documentation (read in Chromium on 2026-11-10)

* Multipart upload, "5 MB or less" with metadata in one request: `POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart`, a `multipart/related` body whose first part is the JSON metadata (`application/json; charset=UTF-8`) and second part the media, ending with two hyphens after the final boundary.
* Resumable upload, "for large files (greater than 5 MB)" and "a good choice for most applications": a `POST` with `uploadType=resumable` returns `200` and a `Location` session URI with an empty body, then the bytes go in a `PUT`. After an interruption "create an empty PUT request to the resumable session URI" with `Content-Range: bytes */TOTAL`: `200` or `201` means done, `308` with a `Range` header says which bytes the server holds, `404` means the session expired and the upload restarts. The client resumes from the byte after `Range` and restarts the whole step on a `404`.
* "To update an existing file, use PATCH", the same two upload types on `/upload/drive/v3/files/{id}`.
* Errors: "use exponential backoff" for `5xx`, `429` and a `403` with the reason `rateLimitExceeded` or `userRateLimitExceeded`. Any other `4xx`, a `403` for permissions included, is not retried.

The retry unit is the module's own choice. What is retried is the whole find then write step, not the single request. A create whose answer was lost (the file exists, the response never arrived) is found by name on the next look and is `unchanged`, not created a second time. A test closes the connection after the server created the file to prove it.

## Credentials

`GOOGLE_DRIVE_TOKEN_FILE` points at an authorized-user token file (`token`, `refresh_token`, `client_id`, `client_secret`, `token_uri`, `expiry`). The access token is renewed with the refresh grant when it is expired or within a minute of expiry, and once more if a call answers `401`. The file is read, never written back, so another tool that shares it is unaffected. `GOOGLE_DRIVE_ACCESS_TOKEN` is a fixed token for a short job and cannot renew. `GOOGLE_API_BASE` points the client at the stand-in server. No token, refresh token or client secret reaches a log line, an exception message or a repr, and an unreadable token file is reported without its path.

## Tests

```
python -m pytest tests/test_drive.py -q            # 89 tests, about 4 seconds, no network
python tests/mutation_check_drive.py --check-targets
python tests/mutation_check_drive.py               # edits app/report/drive.py in place: run it in a copy
```

The stand-in is `tests/drive_stand_in.py`: the documented endpoints, the documented error shape, and faults on demand (429 with `Retry-After`, 503, a rate limit 403, a permissions 403, a connection closed before or after the work, a resumable upload cut in half, an expired session, a refused token). The suite covers create, replace, unchanged, duplicates, the trash, another folder, an escaped name, paging, both upload types and their boundary at exactly 5 MB, resume, retries and their cap, token renewal, secrets, the settings and the script's exit codes, plus 40 seeded random worlds that mix faults into several deliveries and require there is never a second copy and a success always holds the last bytes.

The mutation check breaks 39 guards one at a time (each must fail the test it names, not just any test): 39 caught, 0 survived. Output with the real run: [test-runs/2026-11-10-drive-delivery.txt](test-runs/2026-11-10-drive-delivery.txt).

## The real run

With the stored OAuth token of a personal Drive, the same `deliver_to_drive` step the script runs: a folder "api-analytics-hub test" made with the existing `gdrive.py`, the sample deck from `docs/monthly-report-sample` delivered three times (created, unchanged, replaced after the bytes changed), the folder searched (one file), the file downloaded and compared byte for byte, a wrong folder id reported, then a forced resumable upload over the same name (replaced, still one file), and the test file deleted. The test folder was moved to the Drive trash afterwards.

## Limits

* The n8n workflow does not call the Drive step. Slack and email are its two deliveries; a Drive node would call this module through the report script or a small endpoint, and neither is built.
* Only a personal OAuth token was used. A service account or a shared drive was not tried; the requests send `supportsAllDrives=true` as the documentation asks for shared drives, and that is untested.
* A deck over 5 MB took the resumable path only in the stand-in and in a forced run of a 100 KB deck, never with a real 5 MB file.
* Delivery is at least once. If the replace succeeds and the answer is lost, the next look finds the new bytes and reports `unchanged`.
