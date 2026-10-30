"""Mutation control: break each load-bearing guard and prove the suite goes red.

A green suite is not evidence. On a previous project 48 passing tests proved
nothing about whether the service could boot, because the tests and the code
shared the same wrong assumption. The only way to know a guard is tested is to
delete it and watch something fail.

Each mutation below targets a decision that is SILENTLY wrong when removed: no
crash, no error, just a number that is off in a way nobody notices until a client
queries their own revenue.

THIS EDITS SOURCE FILES IN PLACE. It writes each mutation into the real file, runs the
tests, and writes the original bytes back. Run it in a COPY of the repository, never in a
working tree you are editing or that a sync might commit: a killed process (power loss, kill
-9) can leave a weakened guard in the source, and `git diff` is how to check. A normal exit,
Ctrl-C and SIGTERM all restore every file first.

    python tests/mutation_check.py --check-targets      only confirm every guard finds its text
    python tests/mutation_check.py --only report,deck    only the guards aimed at those keys
    python tests/mutation_check.py --match comma,flat    only the guards whose name holds a word
    python tests/mutation_check.py                       every guard (about an hour)

An argument it does not know is refused with exit 2, and nothing is edited.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python() -> Path:
    """The repository venv's interpreter: Scripts on Windows, bin elsewhere, else the running one.

    Only the Windows form has been run through the whole harness; the other is the standard
    layout of a venv made with `python -m venv .venv`.
    """
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = venv_python()

TARGETS = {
    "aicopy": ROOT / "app" / "ai" / "catalog_copy.py",
    "crypto": ROOT / "app" / "crypto.py",
    "auth": ROOT / "app" / "api" / "auth.py",
    "apimain": ROOT / "app" / "api" / "main.py",
    "db": ROOT / "app" / "db.py",
    "shopify": ROOT / "app" / "connectors" / "shopify.py",
    "base": ROOT / "app" / "connectors" / "base.py",
    "tokens": ROOT / "app" / "connectors" / "tokens.py",
    "webhooks": ROOT / "app" / "connectors" / "shopify_webhooks.py",
    "registry": ROOT / "app" / "connectors" / "registry.py",
    "obs": ROOT / "app" / "obs.py",
    "health": ROOT / "app" / "health.py",
    "m004": ROOT / "migrations" / "004_observability.sql",
    "m005": ROOT / "migrations" / "005_change_detection.sql",
    "changes": ROOT / "app" / "changes.py",
    "cli": ROOT / "scripts" / "load_carrier_snapshot.py",
    "report": ROOT / "app" / "report" / "monthly.py",
    "insights": ROOT / "app" / "report" / "insights.py",
    "deck": ROOT / "app" / "report" / "deck.py",
    "ga4": ROOT / "app" / "connectors" / "ga4.py",
    "web": ROOT / "app" / "report" / "web.py",
    "service": ROOT / "app" / "report" / "service.py",
    "m006": ROOT / "migrations" / "006_ga4_metrics.sql",
    "m007": ROOT / "migrations" / "007_meta_metrics.sql",
    "m008": ROOT / "migrations" / "008_search_metrics.sql",
    "m009": ROOT / "migrations" / "009_report_runs.sql",
}

MUTATIONS = [
    (
        "aicopy",
        "grounding check disabled (fabricated specs get published)",
        "    for raw in NUMBER_RE.findall(text):\n        if _normalise(raw) not in haystack:\n            unsupported.append(raw)",
        "    pass",
        "an invented number must never reach the catalogue",
    ),
    (
        "aicopy",
        "written numbers no longer checked (model says 'five' not 5)",
        '        if word in WORD_NUMBERS and word not in haystack:\n            unsupported.append(word)',
        "        pass",
        "a spelled-out number must be checked like a digit",
    ),
    (
        "aicopy",
        "generation attempted with no facts at all",
        '        if not facts_text.strip():',
        "        if False:",
        "no facts means nothing truthful can be written",
    ),
    (
        "aicopy",
        "truncated completion reported as a content problem",
        '        if choice.get("finish_reason") == "length" and not content:',
        "        if False:",
        "a truncation must not be blamed on the copy",
    ),
    (
        "auth",
        "rbac permission check removed (every role may do everything)",
        "        if not principal.may(permission):",
        "        if False:",
        "a viewer must not be able to trigger a sync",
    ),
    (
        "auth",
        "revoked keys keep working",
        "        if row[\"revoked_at\"] is not None:",
        "        if False:",
        "revoking a key must actually stop it",
    ),
    (
        "auth",
        "an unknown role falls back to access instead of failing closed",
        "            raise HTTPException(\n                status_code=status.HTTP_403_FORBIDDEN, detail=\"unknown role\"\n            )",
        "            role = Role.VIEWER",
        "an unrecognised role must fail closed",
    ),
    (
        "apimain",
        "sync skips the ownership check (write across the tenant boundary)",
        "    if not owned:",
        "    if False:",
        "one tenant must not start a sync on another tenant's connection",
    ),
    (
        "apimain",
        "arbitrary window sizes accepted (unbounded table scan)",
        "    if days not in ALLOWED_WINDOWS:",
        "    if False:",
        "the window must come from an allow-list",
    ),
    (
        "db",
        "tenant GUC set at session scope instead of transaction scope",
        "\"select set_config('app.tenant_id', $1, true)\", str(tenant_id)",
        "\"select set_config('app.tenant_id', $1, false)\", str(tenant_id)",
        "the tenant must not leak onto the next request through a pooled connection",
    ),
    (
        "crypto",
        "aad dropped from encryption (ciphertext no longer bound to its row)",
        'return bytes([self._current]) + nonce + blob',
        'blob = AESGCM(self._keys[self._current]).encrypt(nonce, plaintext.encode(), b"")\n        return bytes([self._current]) + nonce + blob',
        "a ciphertext must not decrypt in another tenant's row",
    ),
    (
        "crypto",
        "fixed nonce (GCM nonce reuse breaks confidentiality and integrity)",
        'nonce = secrets.token_bytes(NONCE_BYTES)',
        'nonce = b"0" * NONCE_BYTES',
        "each encryption must use a fresh nonce",
    ),
    (
        "crypto",
        "Secret.__format__ redacts instead of raising",
        'raise TypeError("refusing to format a Secret, call .reveal() explicitly")',
        'return "***"',
        "formatting a secret must fail loudly, not hide the mistake",
    ),
    (
        "crypto",
        "key length no longer validated at construction",
        'if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:',
        'if False:',
        "a wrong-length key must be refused where it enters",
    ),
    (
        "shopify",
        "protected-data denial flattened into a revoked token",
        'if "protected-customer-data" in doc or "protected customer data" in message.lower():\n                    raise ProtectedDataDenied(message)',
        'pass',
        "the two ACCESS_DENIED kinds must stay distinct",
    ),
    (
        "shopify",
        "withheld customers metric reported as zero",
        'if self._customer_field_allowed:\n                points.append(\n                    CanonicalPoint("customers", day, len(b["customers"]), None,\n                                   {"note": "distinct customers on this day, not lifetime"})\n                )',
        'points.append(\n                CanonicalPoint("customers", day, len(b["customers"]), None,\n                               {"note": "distinct customers on this day, not lifetime"})\n            )',
        "a withheld metric must vanish, never read zero",
    ),
    (
        "shopify",
        "degradation retry not latched (refused call per page)",
        'self._customer_field_allowed = False\n                self.degraded_metrics.add("customers")',
        'self.degraded_metrics.add("customers")',
        "the retry must cost one call, not one per page",
    ),
    (
        "shopify",
        "float money conversion (drops Decimal exactness)",
        'exponent = 0 if currency.upper() in _ZERO_DECIMAL else 2',
        'exponent = 2',
        "zero-decimal currencies must not be scaled",
    ),
    (
        "shopify",
        "no bucket reserve (runs the rate limiter to zero)",
        "BUCKET_RESERVE_RATIO = 0.33",
        "BUCKET_RESERVE_RATIO = 0.0",
        "pacing must hold headroom back",
    ),
    (
        "shopify",
        "no UTC normalisation (books orders on the local date)",
        '.astimezone(\n                    timezone.utc\n                ).date()',
        '.date()',
        "day bucketing must be UTC",
    ),
    (
        "shopify",
        "multi-currency totals silently summed",
        'raise UpstreamBroken(\n                            f"two currencies on {day}: {bucket[\'currency\']} and {currency}"\n                        )',
        'pass',
        "two currencies in a day must refuse",
    ),
    (
        "base",
        "money without a currency accepted",
        'raise UpstreamBroken(f"{self.metric_key} is money and needs a currency")',
        'pass',
        "money must carry a currency",
    ),
    (
        "tokens",
        "no lock around refresh (concurrent workers each mint a token)",
        "async with self._lock:",
        "if True:",
        "concurrent refresh must collapse to one",
    ),
    (
        "tokens",
        "no skew margin (refreshes only after the token is already dead)",
        "REFRESH_SKEW_SECONDS = 120.0",
        "REFRESH_SKEW_SECONDS = 0.0",
        "a nearly-expired token must be treated as expired",
    ),
    (
        "webhooks",
        "verification skipped entirely (forged deliveries accepted)",
        "        verify_signature(raw_body, headers, self.secret)",
        "        pass",
        "an unverified delivery must be rejected before anything else",
    ),
    (
        "webhooks",
        "ascii guard dropped (non-ascii header becomes a 500)",
        '    if not provided.isascii():\n        raise WebhookRejected("signature header is not ascii")',
        "    pass",
        "a non-ascii signature header must reject cleanly, not raise TypeError",
    ),
    # NOT MUTATED, deliberately: replacing compare_digest with `!=` still rejects
    # forgeries and only leaks timing, so no functional test can see the
    # difference. A green suite under that mutation would be a harness defect
    # rather than a missing test, and faking a timing assertion here would be
    # worse than leaving it uncovered. The constant-time compare stays because it
    # is correct, not because a test forces it.
    (
        "webhooks",
        "delivery log unbounded (memory leak in a long-lived receiver)",
        "        while len(self._seen) > self.capacity:",
        "        while False:",
        "the delivery log must evict",
    ),
    (
        "registry",
        "unsupported metric no longer raises",
        "            raise MetricNotSupported(",
        "            pass  # mutated\n            _ = MetricNotSupported(",
        "asking a platform for a metric it cannot produce must raise",
    ),
    # ------------------------------------------------------------------ observability
    (
        "obs",
        "a non-ASCII request id is reused in the log",
        'all(c.isascii() and (c.isalnum() or c in "-_") for c in incoming)',
        'all(c.isalnum() or c in "-_" for c in incoming)',
        "an accented header value must not become a log field",
    ),
    (
        "obs",
        "a newline in a label value is not escaped",
        '.replace("\\n", "\\\\n")',
        "",
        "a label value must not be able to start a new metric line",
    ),
    (
        "obs",
        "an exception logs its message instead of its class",
        'out["error"] = record.exc_info[0].__name__',
        'out["error"] = str(record.exc_info[1])',
        "an exception message can carry a connection string or a key",
    ),
    (
        "obs",
        "histogram buckets stop being cumulative",
        "                running += n\n                lines.append('hub_http_request_seconds_bucket{route=\"%s\",le=\"%s\"} %d'",
        "                running = n\n                lines.append('hub_http_request_seconds_bucket{route=\"%s\",le=\"%s\"} %d'",
        "a Prometheus histogram bucket counts everything at or below its bound",
    ),
    (
        "health",
        "an unchanged problem is never repeated",
        'if keys != old or now - float(state.get("alerted_at", 0.0)) >= repeat_seconds:',
        "if keys != old:",
        "a problem nobody fixed must come back after the quiet window",
    ),
    (
        "health",
        "a cleared set sends no recovery",
        'return ("Recovered. " + describer([]) if old else None), {"keys": [], "alerted_at": 0.0}',
        'return None, {"keys": [], "alerted_at": 0.0}',
        "the channel must be told when the problem is gone",
    ),
    (
        "apimain",
        "a failed database read is reported as healthy",
        "        ok = False\n        log.error(\"metrics scrape",
        "        ok = True\n        log.error(\"metrics scrape",
        "a metric that could not be read must say so instead of looking like zero",
    ),
    (
        "apimain",
        "the optional metrics token is ignored",
        "        return Response(status_code=401)\n    gauges = None",
        "        pass\n    gauges = None",
        "a configured scrape token must be enforced",
    ),
    (
        "apimain",
        "the raw path becomes the route label",
        'route = getattr(request.scope.get("route"), "path", None) or "unmatched"',
        "route = request.url.path",
        "a path with an id in it must not create a series or reach a log",
    ),
    (
        "auth",
        "the tenant id is no longer attached to the request",
        "    request.state.tenant_id = principal.tenant_id",
        "    pass",
        "an operator follows one customer's trail by the tenant id in the log",
    ),
    (
        "m004",
        "the stale threshold ignores the connection's own cadence",
        "and now() - l.at > make_interval(secs => greatest(coalesce(s.cadence_seconds * 3, p_default_stale_seconds), 900))",
        "and now() - l.at > make_interval(secs => 21600)",
        "a 15 minute connection is stale after three cadences, not after six hours",
    ),
    (
        "m004",
        "a paused connection is reported as never succeeded",
        "where c.status = 'active' and l.at is null",
        "where l.at is null",
        "a connection somebody paused on purpose is not an incident",
    ),
    (
        "m004",
        "the worker role loses schema usage",
        "grant usage on schema public to sync_worker;",
        "",
        "the worker-role pool must be able to resolve the health functions",
    ),
    # ------------------------------------------------------------------ snapshot change detection (005 and app/changes.py)
    # the hash and the cleaning of cells before it
    (
        "m005",
        "the hash ignores authority_status",
        "        coalesce(authority_status, E'\\x02'),\n",
        "",
        "a carrier whose authority status alone moved must still be examined",
    ),
    (
        "m005",
        "the hash ignores the insurer",
        "        coalesce(insurer, E'\\x02'),\n",
        "",
        "an insurer change alone must move the hash",
    ),
    (
        "m005",
        "the hash ignores the filing date",
        "        coalesce((filing_date - date '1970-01-01')::text, E'\\x02'),\n",
        "",
        "each tracked date must move the hash on its own",
    ),
    (
        "m005",
        "the staged hash includes an untracked column (the name)",
        "carrier_track_hash(carrier_clean_code(insurer_raw), ",
        "carrier_track_hash(carrier_clean_code(insurer_raw || legal_name_raw), ",
        "a name edit alone must not make a row look changed",
    ),
    (
        "m005",
        "codes are no longer upper-cased before the hash",
        "    select upper(public.carrier_clean_text(raw))",
        "    select public.carrier_clean_text(raw)",
        "a lower case status is the same status",
    ),
    (
        "m005",
        "inner whitespace is no longer collapsed before the hash",
        "regexp_replace(coalesce(raw, ''), '\\s+', ' ', 'g')), '')",
        "coalesce(raw, '')), '')",
        "a doubled space inside a value is not a change",
    ),
    (
        "m005",
        "a date that is not ISO is read under the session DateStyle",
        "    if t !~ '^\\d{4}-\\d{2}-\\d{2}$' then raise exception 'not an ISO date: %', left(t, 40); end if;\n",
        "",
        "an ambiguous date must fail the load, not be read as another day",
    ),
    # keys
    (
        "m005",
        "the event natural key is no longer unique",
        "    constraint change_event_natural_key unique (usdot_norm, kind, event_ref)",
        "    constraint change_event_natural_key check (true)",
        "the database itself must refuse a second alert for the same filing",
    ),
    (
        "m005",
        "the MC normaliser keeps leading zeros",
        "nullif(ltrim(regexp_replace(regexp_replace(coalesce(raw, ''), '\\.0+\\s*$', ''), '\\D', '', 'g'), '0'), '') as d",
        "nullif(regexp_replace(regexp_replace(coalesce(raw, ''), '\\.0+\\s*$', ''), '\\D', '', 'g'), '') as d",
        "MC-0012345 and 12345 are one docket",
    ),
    (
        "m005",
        "the USDOT normaliser keeps leading zeros",
        "    select nullif(ltrim(regexp_replace(regexp_replace(coalesce(raw, ''), '\\.0+\\s*$', ''), '\\D', '', 'g'), '0'), '')\n$$;",
        "    select nullif(regexp_replace(regexp_replace(coalesce(raw, ''), '\\.0+\\s*$', ''), '\\D', '', 'g'), '')\n$$;",
        "0090000001 and 90000001 are one carrier",
    ),
    (
        "m005",
        "an FF or MX docket is read as an MC",
        "when upper(coalesce(raw, '')) ~ '^\\s*(FF|MX)' then substring(upper(raw) from '^\\s*(FF|MX)') else 'MC' end",
        "when false then 'MC' else 'MC' end",
        "a freight forwarder docket must never match an MC with the same digits",
    ),
    (
        "m005",
        "history can be rewritten",
        "create trigger carrier_history_append_only before update or delete on carrier_history\n    for each row execute function carrier_forbid_rewrite();",
        "",
        "history is kept forever",
    ),
    (
        "m005",
        "events can be truncated",
        "create trigger change_event_no_truncate before truncate on change_event\n    for each statement execute function carrier_forbid_rewrite();",
        "",
        "the alert record is kept forever",
    ),
    # the daily load
    (
        "changes",
        "the day is not one transaction",
        '    with conn.transaction():\n        cur = conn.cursor()\n        cur.execute("select pg_try_advisory_xact_lock(%s)", (LOAD_LOCK_KEY,))',
        '    with __import__("contextlib").nullcontext():\n        cur = conn.cursor()\n        cur.execute("select pg_try_advisory_xact_lock(%s)", (LOAD_LOCK_KEY,))',
        "a crash must leave no half day",
    ),
    (
        "changes",
        "a repeated alert raises instead of being suppressed",
        "        on conflict on constraint change_event_natural_key do nothing\n",
        "",
        "a flap back to a known state must not fail the load or alert twice",
    ),
    (
        "changes",
        "the new_entity rule is inverted",
        "          from carrier_delta where is_new\n        union all\n        select usdot_norm, mc_norm, 'insurer_changed'",
        "          from carrier_delta where not is_new\n        union all\n        select usdot_norm, mc_norm, 'insurer_changed'",
        "only a carrier not seen before is a new_entity",
    ),
    (
        "changes",
        "the first load raises alerts",
        "baseline = loads == 0",
        "baseline = False",
        "the first file is a baseline and must not flood the outbox",
    ),
    (
        "changes",
        "an older file is applied",
        "if latest is not None and snapshot_date < latest:",
        "if False:",
        "an old file must not roll the carriers back",
    ),
    (
        "changes",
        "two loads may run at once",
        "        if not cur.fetchone()[0]:\n            raise LoadBusy",
        "        if False:\n            raise LoadBusy",
        "the staging table is shared",
    ),
    (
        "changes",
        "duplicate carrier rows are all kept",
        "where s.usdot_norm = d.usdot_norm and s.seq < d.keep",
        "where s.usdot_norm = d.usdot_norm and s.seq < 0",
        "two rows for one carrier cannot both be today's state",
    ),
    (
        "changes",
        "rows with no usable USDOT number are kept",
        'cur.execute("delete from carrier_stage where usdot_norm is null")',
        'cur.execute("select 1")',
        "a row that cannot be tied to a carrier is rejected and counted",
    ),
    (
        "changes",
        "every row is examined, not only those whose hash moved",
        "where c.usdot_norm is null or c.track_hash is distinct from s.track_hash",
        "where true",
        "the hash is what keeps 400,000 rows down to a few thousand",
    ),
    (
        "changes",
        "the header is not checked",
        "    if got != FILE_COLUMNS:",
        "    if False:",
        "a file with the columns in another order must not load",
    ),
    # refusing a bad file
    (
        "changes",
        "a file with a header and no rows is not refused as empty",
        '        if cur.fetchone()[0] == 0:\n            raise LoadRefused("the file has a header and no data rows")',
        '        if False:\n            raise LoadRefused("the file has a header and no data rows")',
        "a header alone is an empty file",
    ),
    (
        "changes",
        "a truncated file is applied",
        "if not baseline and (absent_is_event or not force) and held > 0 and staged < min_staged_ratio * held:",
        "if False:",
        "a file with under half the carriers must be refused",
    ),
    (
        "changes",
        "a file where most rows moved is applied",
        "if not baseline and not force and changed > max_changed_ratio * staged:",
        "if False:",
        "a file where over a quarter moved is a bad file",
    ),
    (
        "changes",
        "force does not skip the changed-rows guard",
        "if not baseline and not force and changed > max_changed_ratio * staged:",
        "if not baseline and changed > max_changed_ratio * staged:",
        "force must let a real mass change through",
    ),
    (
        "changes",
        "a date far in the future is accepted",
        "if not force and (snapshot_date - today).days > 1:",
        "if False:",
        "a typo in the date would block every later file",
    ),
    (
        "changes",
        "absent carriers are not counted",
        'cur.execute("select count(*) from carrier_current c where not exists (select 1 from carrier_stage s where s.usdot_norm = c.usdot_norm)")',
        'cur.execute("select 0")',
        "carriers a file does not mention must be counted, since they raise no event",
    ),
    (
        "changes",
        "unknown policy status values are not counted",
        "policy_status <> all(%s)",
        "policy_status = any(%s)",
        "a vocabulary change in the feed must show as a number",
    ),
    # event keys
    (
        "changes",
        "an authority change key leaves out the date",
        "coalesce(authority_status, '') || ':' || coalesce((authority_date - date '1970-01-01')::text, '')",
        "coalesce(authority_status, '')",
        "an authority lost twice must alert twice",
    ),
    (
        "changes",
        "a cancellation key leaves out the cancel date",
        "'cancellation_filed', coalesce(filing_id, '') || '|' || coalesce(to_char(coalesce(cancel_date, filing_date), 'YYYY-MM-DD'), '')",
        "'cancellation_filed', coalesce(filing_id, '')",
        "cancelled, reinstated and cancelled again on one filing must alert twice",
    ),
    (
        "changes",
        "an insurer change key ignores the filing",
        "'insurer_changed', coalesce(filing_id, ''), jsonb_build_object",
        "'insurer_changed', '', jsonb_build_object",
        "two insurer changes on two filings are two alerts",
    ),
    # matching
    (
        "changes",
        "events are never matched by MC number",
        "from change_event e join carrier_company c on c.mc_norm = e.mc_norm where {where}",
        "from change_event e join carrier_company c on false where {where}",
        "a company known only by its MC number must match",
    ),
    (
        "changes",
        "events are never matched by USDOT number",
        "from change_event e join carrier_company c on c.usdot_norm = e.usdot_norm where {where}",
        "from change_event e join carrier_company c on false where {where}",
        "a company known only by its USDOT number must match",
    ),
    (
        "changes",
        "a carrier matching two companies is not flagged as a conflict",
        "case when count(*) > 1 then 'conflict' else 'matched' end",
        "case when count(*) > 9 then 'conflict' else 'matched' end",
        "two companies for one carrier is for a person to decide",
    ),
    (
        "changes",
        "an event that matched no company is queued anyway",
        "          join change_event_match m on m.event_id = e.id\n          join carrier_company c on c.id = m.company_id\n         where {where}",
        "          left join change_event_match m on m.event_id = e.id\n          left join carrier_company c on c.id = m.company_id\n         where {where}",
        "only an event with a company is pushed",
    ),
    (
        "changes",
        "a late company is matched to events of any age",
        'where = "e.created_at >= now() - make_interval(days => %s) and c.id = %s"',
        'where = "%s::int is not null and c.id = %s"',
        "the late match looks back N days and no further",
    ),
    (
        "changes",
        "a late company can queue an event twice",
        "        on conflict (event_id) do nothing\n",
        "",
        "an event already queued is never queued again",
    ),
    # the outbox
    (
        "changes",
        "a row is marked delivered when it is claimed, before any outcome",
        "attempts = o.attempts + 1, last_attempt_at = now()",
        "attempts = o.attempts + 1, last_attempt_at = now(), status = 'delivered'",
        "delivered means the sink answered ok and the answer is stored",
    ),
    (
        "changes",
        "an ambiguous answer counts as delivered",
        'if answer is not None and answer.status == "ok":',
        'if answer is not None and answer.status in ("ok", "ambiguous"):',
        "an answer that may not have acted is not a delivery",
    ),
    (
        "changes",
        "an answer the sink should not give counts as delivered",
        'answer = SinkAnswer("ambiguous", None, "unrecognised answer")',
        'answer = SinkAnswer("ok", None, None)',
        "an unrecognised answer is retried, never trusted",
    ),
    (
        "changes",
        "a live claim does not stop a second worker",
        " and (lease_expires_at is null or lease_expires_at <= now())",
        "",
        "two workers must not push the same row",
    ),
    (
        "changes",
        "a stale worker's ok is stored",
        "where event_id = %s and claim_token = %s and status = 'pending' returning status\",\n                (answer.ref,",
        "where event_id = %s and (claim_token = %s or true) and status = 'pending' returning status\",\n                (answer.ref,",
        "a worker that lost its claim must not mark the row delivered",
    ),
    (
        "changes",
        "a stale worker's failure is stored",
        " where event_id = %s and claim_token = %s and status = 'pending' returning status\",\n                (max_attempts,",
        " where event_id = %s and (claim_token = %s or true) and status = 'pending' returning status\",\n                (max_attempts,",
        "a worker that lost its claim must not release the new worker's row",
    ),
    (
        "changes",
        "a row never goes dead",
        "when attempts >= %s then 'dead'",
        "when attempts >= %s + 1000 then 'dead'",
        "a row that keeps failing stops being pushed",
    ),
    (
        "changes",
        "a rejected answer is retried",
        'elif answer is not None and answer.status == "rejected":',
        "elif False:",
        "a refusal is permanent",
    ),
    (
        "changes",
        "the sink's exception message is stored",
        'answer = SinkAnswer("ambiguous", None, type(exc).__name__)',
        'answer = SinkAnswer("ambiguous", None, str(exc))',
        "an exception message can carry a webhook URL with a token",
    ),
    (
        "changes",
        "a failed push is retried with no backoff",
        "least(%s * power(2, attempts - 1), %s)",
        "least(%s * 0, %s)",
        "a failing sink must not be hammered",
    ),
    (
        "changes",
        "a throttled answer spends an attempt",
        "set attempts = greatest(attempts - 1, 0), next_attempt_at",
        "set attempts = attempts, next_attempt_at",
        "being told to slow down is not a failure",
    ),
    (
        "changes",
        "retry_after is ignored",
        "wait = backoff_seconds if answer.retry_after is None else answer.retry_after",
        "wait = backoff_seconds",
        "the sink's own wait is honoured",
    ),
    (
        "changes",
        "a requeued row keeps its spent attempts",
        "set status = 'pending', attempts = 0,",
        "set status = 'pending', attempts = attempts,",
        "a requeued row gets a fresh set of attempts",
    ),
    (
        "changes",
        "a claim is not committed on a normal connection",
        '    with conn.transaction(), conn.cursor() as cur:\n        cur.execute(\n            """\n            update change_outbox o',
        '    with conn.cursor() as cur:\n        cur.execute(\n            """\n            update change_outbox o',
        "a worker's claim must be visible before the sink is called",
    ),
    (
        "changes",
        "an outcome is not committed on a normal connection",
        '    with conn.transaction(), conn.cursor() as cur:\n        if answer is not None and answer.status == "ok":',
        '    with conn.cursor() as cur:\n        if answer is not None and answer.status == "ok":',
        "a lost outcome means the same alert is pushed again blind",
    ),
    # the report
    (
        "changes",
        "the report counts every date's events",
        'where snapshot_date = %s group by kind", (snapshot_date,))',
        'where %s::date is not null group by kind", (snapshot_date,))',
        "a day's report is that day's events",
    ),
    (
        "changes",
        "the report lists changes in id order, not by urgency",
        "order by array_position(%s::text[], e.kind), e.id",
        "order by e.id + 0 * array_position(%s::text[], e.kind)",
        "a cancellation is read before a new carrier",
    ),
    # absence as news (optional mode), the previous-load comparison, the external key, the command line
    (
        "changes",
        "absence raises events even with the mode off",
        '    """ if absent_is_event else ""',
        '    """ if True else ""',
        "the default is silent: a carrier that is not in the file raises nothing",
    ),
    (
        "changes",
        "a carrier that stays absent raises an event every day",
        "         where c.absent_load is null and not exists (select 1 from carrier_stage s where s.usdot_norm = c.usdot_norm)\n    \"\"\" if absent_is_event",
        "         where not exists (select 1 from carrier_stage s where s.usdot_norm = c.usdot_norm)\n    \"\"\" if absent_is_event",
        "the second absent day raises nothing",
    ),
    (
        "changes",
        "an absent carrier is never marked",
        "where cu.absent_load is null and not exists",
        "where false and not exists",
        "the mark is what keeps the second absent day silent",
    ),
    (
        "changes",
        "a carrier that returns is not examined",
        "or c.track_hash is distinct from s.track_hash or c.absent_load is not null",
        "or c.track_hash is distinct from s.track_hash",
        "a return goes through the history path and clears the mark",
    ),
    (
        "changes",
        "a returning carrier keeps its absent mark",
        "last_changed_load = %s, absent_load = null",
        "last_changed_load = %s, absent_load = c.absent_load",
        "a carrier that comes back and vanishes again alerts again",
    ),
    (
        "changes",
        "the vanish key leaves out the load, so a second vanish is suppressed",
        "coalesce(c.filing_id, '') || ':' || c.last_changed_load,",
        "coalesce(c.filing_id, ''),",
        "vanish, return, vanish is two alerts",
    ),
    (
        "changes",
        "force skips the floor in absent mode",
        "if not baseline and (absent_is_event or not force) and held > 0",
        "if not baseline and not force and held > 0",
        "a truncated file must never read as thousands of endings",
    ),
    (
        "changes",
        "the previous load's row count is not recorded",
        "(select rows_staged from carrier_load order by id desc limit 1)",
        "null::int",
        "a shrinking file must be visible in the report",
    ),
    (
        "changes",
        "the payload carries no external key",
        "'external_key', e.usdot_norm || ':' || e.kind || ':' || e.event_ref, ",
        "",
        "the sink upserts on the external key",
    ),
    (
        "cli",
        "the command line ignores --force",
        "force=a.force,",
        "force=False,",
        "force must reach the load",
    ),
    (
        "cli",
        "the command line ignores --absent-is-event",
        "absent_is_event=a.absent_is_event,",
        "absent_is_event=False,",
        "the flag must reach the load",
    ),
    (
        "cli",
        "a refused file exits 0",
        "        return 3\n",
        "        return 0\n",
        "a cron job must be told when a file was refused",
    ),
    # output round 1: a connection inside a transaction, a re-filed alert, a spreadsheet number, the loader's reason, the report's rejected rows
    (
        "changes",
        "apply_snapshot accepts a connection inside a transaction",
        '    _require_idle(conn, "apply_snapshot")\n',
        "",
        "a load on a connection inside a transaction would be rolled back at close and the day lost without a word",
    ),
    (
        "changes",
        "claim_outbox accepts a connection inside a transaction",
        '    _require_idle(conn, "claim_outbox")\n',
        "",
        "a claim that is only a savepoint is invisible to a second worker, which then pushes the same alert",
    ),
    (
        "changes",
        "record_outcome accepts a connection inside a transaction",
        '    _require_idle(conn, "record_outcome")\n',
        "",
        "an outcome that is only a savepoint is lost with the connection and the next run pushes the alert again",
    ),
    (
        "changes",
        "the daily report leaves a normal connection inside a transaction",
        "    with conn.transaction(), conn.cursor() as cur:\n        if snapshot_date is None:",
        "    with conn.cursor() as cur:\n        if snapshot_date is None:",
        "printing the report must not turn the next claim and outcome into savepoints",
    ),
    (
        "changes",
        "a second cancellation filing on a cancelled carrier does not alert",
        "(old_policy_status is distinct from 'CANCELLED' or filing_id is distinct from old_filing_id)",
        "old_policy_status is distinct from 'CANCELLED'",
        "an owner must hear about a new cancellation filing even when the status stayed CANCELLED between two files",
    ),
    (
        "changes",
        "a second replacement filing on a replaced carrier does not alert",
        "(old_policy_status is distinct from 'REPLACED' or filing_id is distinct from old_filing_id)",
        "old_policy_status is distinct from 'REPLACED'",
        "an owner must hear about a new replacement filing even when the status stayed REPLACED between two files",
    ),
    (
        "m005",
        "a trailing .0 on a USDOT number becomes a digit",
        r"    select nullif(ltrim(regexp_replace(regexp_replace(coalesce(raw, ''), '\.0+\s*$', ''), '\D', '', 'g'), '0'), '')" + "\n",
        r"    select nullif(ltrim(regexp_replace(coalesce(raw, ''), '\D', '', 'g'), '0'), '')" + "\n",
        "a spreadsheet export that wrote 90005001.0 must stay the same carrier, not become 900050010",
    ),
    (
        "m005",
        "a trailing .0 on a docket number becomes a digit",
        r"nullif(ltrim(regexp_replace(regexp_replace(coalesce(raw, ''), '\.0+\s*$', ''), '\D', '', 'g'), '0'), '') as d) t",
        r"nullif(ltrim(regexp_replace(coalesce(raw, ''), '\D', '', 'g'), '0'), '') as d) t",
        "a spreadsheet export that wrote MC-713740.0 must stay the same docket",
    ),
    (
        "cli",
        "the loader prints only an exception class when a load cannot run",
        '        print("could not load: %s: %s" % (type(exc).__name__, _why(exc, dsn)), file=sys.stderr)\n',
        '        print("could not load: %s" % type(exc).__name__, file=sys.stderr)\n',
        "an operator and a failed cron run must see which line, cell or credential is wrong",
    ),
    (
        "changes",
        "the report hides rejected and repeated rows",
        '    if report["rows_rejected"] or report["rows_duplicate"]:\n',
        '    if False:\n',
        "a column shift that drops carriers must show in the daily report, not only as an unrelated count",
    ),
    (
        "changes",
        "the report hides a second load of the same date",
        '    if report["loads_on_date"] > 1:\n',
        '    if False:\n',
        "a corrected file published on the same date must not read as one baseline with 119 alerts",
    ),
    (
        "cli",
        "the loader prints the text of an unparseable connection string",
        '        return "the connection string in DATABASE_URL could not be parsed (its text is not printed)"\n',
        "        pass\n",
        "a password with a bare percent sign must not reach cron mail or a log through the parse error",
    ),
    # the monthly report: days, baselines, boundaries, grounding, the deck
    (
        "report",
        "a day nothing covered is counted as a quiet day",
        "        elif _covered(day, covered):\n            stats.quiet_days.append(day)\n        else:\n            stats.missing_days.append(day)",
        "        elif _covered(day, covered):\n            stats.quiet_days.append(day)\n        else:\n            stats.quiet_days.append(day)",
        "a day with no snapshot and no sync must read as missing, never as zero orders",
    ),
    (
        "report",
        "a zero baseline reads as a 100 percent change",
        "    if previous == 0:\n        return None",
        "    if previous == 0:\n        return Decimal(100)",
        "a change from zero has no percentage",
    ),
    (
        "report",
        "rows outside the month are summed into it",
        "        if not (first <= day <= last_complete):\n            continue",
        "        if False:\n            continue",
        "the last day of one month must not land in the next",
    ),
    (
        "report",
        "the percent change divides by the signed previous value",
        "/ abs(Decimal(previous))",
        "/ Decimal(previous)",
        "an improvement from a negative month must stay positive",
    ),
    (
        "report",
        "two rows for one day and metric are summed",
        '            raise ReportError("two rows for %s on %s: report one connection at a time" % (key, day))',
        "            pass",
        "two connections must not be added into one doubled total",
    ),
    (
        "report",
        "a month that was never synced is compared as a month of zero",
        "    if not prev.has_baseline:\n        prev = None",
        "    if False:\n        prev = None",
        "no previous month and a previous month of zero are different facts",
    ),
    (
        "report",
        "a missing day is charted as zero",
        "        elif day in stats.missing_days:\n            out.append((day, None))",
        "        elif day in stats.missing_days:\n            out.append((day, 0))",
        "a missing day must be a gap in the line, not a dip to zero",
    ),
    (
        "insights",
        "the exact figure check is dropped",
        "            elif fig in global_figs and _DAY_COUNT_RE.match(sentence, end):\n                continue\n            else:\n                flag(raw)",
        "            else:\n                continue",
        "a number hiding inside a longer fact must still be refused",
    ),
    (
        "insights",
        "a figure beside a named metric is checked against every metric's figures",
        "        allowed_metric = set().union(*(groups[g] for g in mentioned)) if mentioned else set()",
        "        allowed_metric = everything",
        "net sales must not be allowed the refunds figure",
    ),
    (
        "insights",
        "a comma that is not a thousands separator is not checked",
        '    for m in _BAD_COMMA_RE.finditer(text):',
        '    for m in _BAD_COMMA_RE.finditer(""):',
        "1,4% must not be read as 14 or pass as 1.4",
    ),
    (
        "insights",
        "number words are found by substring",
        '    fact_words = set(_WORD_RE.findall(" ".join(str(v) for v in facts.values()).lower()))',
        '    fact_words = " ".join(str(v) for v in facts.values()).lower()',
        "one is not found inside none",
    ),
    (
        "insights",
        "a day number is accepted in a sentence about a metric",
        "elif fig in global_figs and _DAY_COUNT_RE.match(sentence, end):",
        "elif fig in global_figs:",
        "a bare day count must not stand in for a metric figure",
    ),
    (
        "insights",
        "a refused model text is used anyway",
        "    if reason:\n        return _fallback(",
        "    if False:\n        return _fallback(",
        "text with a number that is not a computed figure must never reach the deck",
    ),
    (
        "insights",
        "the model's line cap is not enforced",
        "    if len(lines) > MAX_LINES:",
        "    if False:",
        "a model essay must not become the commentary",
    ),
    (
        "deck",
        "the data health slide hides the missing days",
        '    notes = ["Days with no data in %s: %s." % (cur.short, days_text(cur.missing_days))]',
        "    notes = []",
        "the days a reader cannot trust must be named on the slide",
    ),
    (
        "deck",
        "a day with no data is not marked on the chart",
        "        if stats is not None and stats.missing_days:",
        "        if False:",
        "a day with no orders and a day with no data must look different",
    ),
    (
        "deck",
        "the library template's extended properties are kept",
        "    _set_app_properties(prs)\n    return prs",
        "    return prs",
        "the file must not claim to be a Mac PowerPoint with no slides",
    ),
    (
        "report",
        "a title states a change without its missing day caveat",
        '    suffix = " (%s)" % clause if clause else ""',
        '    suffix = ""',
        "a fall must not be stated as fact while days are missing",
    ),
    (
        "report",
        "a change that rounds to zero is called unchanged",
        '    if row.unchanged:\n        return "unchanged"',
        '    if row.unchanged or row.flat:\n        return "unchanged"',
        "unchanged means the two exact values are equal",
    ),
    (
        "report",
        "the partial data note is dropped when a baseline note comes first",
        '        if partial and c is not None:\n            notes.append("partial data")',
        '        if partial and c is not None and not notes:\n            notes.append("partial data")',
        "a month with a missing day marks its figures partial whatever else is true of them",
    ),
    (
        "report",
        "the buyers percent is computed from the rounded means",
        "            pct = pct_change(cur_exact[key], prev_exact[key])",
        "            pct = pct_change(c, p)",
        "a percent must not be computed from a rounded figure",
    ),
    (
        "report",
        "today defaults to the machine's local date",
        "    today = as_of or utc_today()",
        "    today = as_of or date.today()",
        "days are bucketed by their UTC date",
    ),
    (
        "report",
        "the UTC date is read from the local clock",
        "    return datetime.now(timezone.utc).date()",
        "    return datetime.now().date()",
        "the report date must not move with the machine's timezone",
    ),
    # the per reported day headline, the chart dots, the metric and currency checks, the model's lines
    (
        "report",
        "a total's headline states a direction from the raw totals alone",
        '    if states_total_with_per_day(report, key):\n        return "%s %s total is %s, but %s: per reported day it is %s" % (',
        '    if False:\n        return "%s %s total is %s, but %s: per reported day it is %s" % (',
        "a total over 29 days against one over 30 can fall while every reported day rose",
    ),
    (
        "report",
        "per reported day divides by the days in the month instead of the days with data",
        "    c = Decimal(cur.totals[key]) / cur.reported_days",
        "    c = Decimal(cur.totals[key]) / cur.days_in_month",
        "the comparison per reported day uses the days a sync looked at",
    ),
    (
        "report",
        "average order value is truncated instead of rounded half up",
        '        return int((Decimal(self.totals["gross_sales"]) / orders).quantize(Decimal(1), rounding=ROUND_HALF_UP))',
        '        return self.totals["gross_sales"] // orders',
        "1999 cents over two orders is 10.00, not 9.99",
    ),
    (
        "report",
        "buyers per active day is called unchanged when only the rounded means are equal",
        "        return self.exact_equal is True",
        "        return self.change is not None and self.change == 0",
        "unchanged means the unrounded values are equal",
    ),
    (
        "report",
        "a zero total in a month with no money row prints n/a",
        '            return "0" if int(value) == 0 else "n/a"',
        '            return "n/a"',
        "a synced month with no orders has a total of zero",
    ),
    (
        "deck",
        "the Note column drops the per reported day change",
        '            if row.per_day is not None:\n                parts.append("per reported day %s" % per_day_phrase(row.per_day))',
        '            if False:\n                parts.append("per reported day %s" % per_day_phrase(row.per_day))',
        "the table must show the per reported day change beside a raw total in a partial month",
    ),
    (
        "deck",
        "a day with no orders is a dot on the current line only",
        "    for s_index, idxs, dot_color in dot_specs:",
        "    for s_index, idxs, dot_color in dot_specs[:1]:",
        "the footnote says a dot on the zero line is a day with no orders, on either line",
    ),
    (
        "insights",
        "the template states a total's direction from the raw totals alone",
        "        if states_total_with_per_day(report, key):\n            lines.append(_total_sentence(report, key))",
        "        if False:\n            lines.append(_total_sentence(report, key))",
        "the narrative must give the per reported day change beside a total in a partial month",
    ),
    (
        "insights",
        "a sentence naming no metric may carry any existing figure",
        "        allowed_metric = set().union(*(groups[g] for g in mentioned)) if mentioned else set()",
        "        allowed_metric = set().union(*(groups[g] for g in mentioned)) if mentioned else everything",
        "a second sentence must not borrow the metric of the first",
    ),
    (
        "insights",
        "sales and revenue are not metric words",
        "    for alias, targets in GENERIC_ALIASES.items():",
        "    for alias, targets in {}.items():",
        "Sales rose 33.3% where 33.3% is the orders figure must be refused",
    ),
    (
        "insights",
        "shoppers is not a word for buyers",
        '"shoppers", "shopper count"),',
        '"shopper count"),',
        "a synonym for a metric names that metric",
    ),
    (
        "insights",
        "customers is not a word for buyers",
        '"customers", "customer count",',
        '"customer count",',
        "a synonym for a metric names that metric",
    ),
    (
        "insights",
        "the currency is dropped from an amount's identity",
        '        return "money", "%s %s" % (_CURRENCY_SYMBOLS.get(symbol, symbol), digits)',
        '        return "money", digits',
        "an amount in the wrong currency must be refused",
    ),
    (
        "insights",
        "spelled fractions, positions and counts are not refused",
        '    "first second fourth couple pair trio several").split())',
        '    "").split())',
        "a third of days is a number written as words",
    ),
    (
        "insights",
        "multiplier words are not refused",
        "        if (word in NUMBER_WORDS or _FOLD_WORD_RE.fullmatch(word)) and word not in fact_words:",
        "        if word in NUMBER_WORDS and word not in fact_words:",
        "twofold and tenfold are numbers written as words",
    ),
    (
        "insights",
        "markdown emphasis stays in a model's line",
        '    return re.sub(r"[*_`#>]+", "", line).strip()',
        "    return line.strip()",
        "**Net sales** must not become Net sales** in a slide",
    ),
    (
        "insights",
        "a preamble ending in a colon is kept as an insight",
        '    lines = [ln for ln in lines if ln and not ln.endswith(":")]',
        "    lines = [ln for ln in lines if ln]",
        "Here is the commentary: is not an insight",
    ),
    # the GA4 connector and the website half of the report (the stand-in server and the hand-made website rows see all of them)
    (
        "ga4",
        "the inclusive endDate is not moved back from the half open window end",
        '"endDate": (end - timedelta(days=1)).isoformat()}',
        '"endDate": end.isoformat()}',
        "a window of three days must ask Google for three days, not four",
    ),
    (
        "ga4",
        "the connector never gives up after its last attempt",
        "            last = attempt == self._max_attempts - 1",
        "            last = False",
        "retries must stop at max_attempts, a broken property spends the server error quota",
    ),
    (
        "ga4",
        "a 429 is not retried",
        "                if code != 429 and code < 500:",
        "                if code < 500:",
        "a quota answer is the one a retry with backoff is for",
    ),
    (
        "ga4",
        "a Retry-After from the server is ignored",
        "        if retry_after is not None:\n            return retry_after",
        "        if False:\n            return retry_after",
        "a wait the server asks for is the wait to use",
    ),
    (
        "ga4",
        "a wait longer than the cap is slept instead of handed to the runner",
        "            if delay > self._backoff_cap:",
        "            if False:",
        "a coroutine must not sleep for an hour",
    ),
    (
        "ga4",
        "the exponential backoff has no cap",
        "        return min(self._backoff_cap, self._backoff_base * (2 ** attempt)) * (0.5 + self._rng() / 2)",
        "        return self._backoff_base * (2 ** attempt) * (0.5 + self._rng() / 2)",
        "the backoff must stop growing at the cap",
    ),
    (
        "ga4",
        "a rejected token is no longer auth expired",
        "                if code == 401:",
        "                if False:",
        "a 401 means refresh the token, not a broken response",
    ),
    (
        "ga4",
        "no access to a property is no longer auth revoked",
        "                if code == 403:",
        "                if False:",
        "a 403 needs a person to fix the access, not a retry",
    ),
    (
        "ga4",
        "a fractional count is truncated instead of refused",
        " or number != number.to_integral_value()",
        "",
        "5.5 sessions is a broken response, never 5",
    ),
    (
        "ga4",
        "a negative count is accepted",
        " or number < 0:",
        ":",
        "a count below zero is a broken response",
    ),
    (
        "ga4",
        "two rows for one day are accepted",
        "            if day in seen:",
        "            if False:",
        "two rows for a day would be written as one, keeping either",
    ),
    (
        "ga4",
        "a row outside the window is accepted",
        "            if not start <= day < end:",
        "            if False:",
        "a day nobody asked for must not be stored",
    ),
    (
        "ga4",
        "the connector stops after the first page",
        "            done = offset >= total",
        "            done = True",
        "a report of more rows than one page must be paged to the end",
    ),
    (
        "ga4",
        "the hourly token floor is ignored",
        "self.tokens_per_hour_remaining <= self._quota_floor:",
        "self.tokens_per_hour_remaining < 0:",
        "the connector must stop before it spends the hour's last tokens",
    ),
    (
        "ga4",
        "the access token is written into the error",
        '                    raise AuthExpired("the access token was rejected (%s)" % _error_text(response))',
        '                    raise AuthExpired("the access token %s was rejected" % self._bearer())',
        "a credential must never reach an error message",
    ),
    (
        "ga4",
        "the dimension column is not checked",
        '        if dims != ["date"]:',
        "        if False:",
        "a response with other columns must not be read as daily rows",
    ),
    (
        "ga4",
        "a missing metric column is not noticed",
        '        if missing:\n            raise UpstreamBroken("the response has no column for',
        '        if False:\n            raise UpstreamBroken("the response has no column for',
        "a metric Google did not send must not read as a zero",
    ),
    (
        "ga4",
        "a property id that is not a number is accepted",
        "        if not match:",
        "        if False:",
        "a wrong id fails before any call, not as a 403 that reads like a permissions problem",
    ),
    (
        "ga4",
        "a negative resume cursor is accepted",
        "            if offset < 0:",
        "            if False:",
        "a cursor that is not a row offset must be refused before a request",
    ),
    (
        "ga4",
        "key events are asked for under the old name conversions",
        '    ("keyEvents", "key_events"),',
        '    ("conversions", "key_events"),',
        "the schema page lists keyEvents and no conversions metric",
    ),
    (
        "ga4",
        "a sampled report is not marked",
        '        return bool(isinstance(meta, dict) and meta.get("samplingMetadatas"))',
        "        return False",
        "a sampled figure must say so on the stored row",
    ),
    (
        "ga4",
        "a dropped connection is not retried",
        "            except httpx.HTTPError as exc:",
        "            except ZeroDivisionError as exc:",
        "a transport failure is retried like a server error",
    ),
    (
        "ga4",
        "zero attempts are accepted",
        "        if max_attempts < 1:",
        "        if False:",
        "a connector that never calls must be refused at construction",
    ),
    (
        "ga4",
        "a rowCount that is not a number is accepted",
        "            if isinstance(total, bool) or not isinstance(total, int) or total < 0:",
        "            if False:",
        "paging without a row count would stop or loop on a guess",
    ),
    (
        "ga4",
        "a missing environment variable is not named",
        '        if missing:\n            raise ValueError("set %s in the environment"',
        '        if False:\n            raise ValueError("set %s in the environment"',
        "a missing credential must be a plain error naming the variable",
    ),
    (
        "ga4",
        "a date that is not eight digits is read anyway",
        "            if not isinstance(text, str) or not _DATE_RE.match(text):",
        "            if False:",
        "2026101 could be read as November 1, so the shape is checked before the calendar",
    ),
    (
        "web",
        "users are added across days instead of averaged",
        "        month_.users_exact = Decimal(sum(users)) / len(users)",
        "        month_.users_exact = Decimal(sum(users))",
        "distinct users cannot be summed across days",
    ),
    (
        "web",
        "the mean of users is truncated instead of rounded half up",
        "        month_.users_per_active_day = month_.users_exact.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)",
        "        month_.users_per_active_day = Decimal(int(month_.users_exact * 10)) / 10",
        "1.666 users per day is 1.7",
    ),
    (
        "web",
        "a day with no traffic counts as unreported for the order rate",
        "    return set(stats.data_days) | set(stats.quiet_days)",
        "    return set(stats.data_days)",
        "a quiet day is a real zero and belongs on both sides of the rate",
    ),
    (
        "web",
        "the order rate counts days only one source reported",
        "    return sorted(_reported(shop) & _reported(web))",
        "    return sorted(_reported(shop) | _reported(web))",
        "a sum over unequal days is not a rate",
    ),
    (
        "web",
        "the order rate with no sessions is zero instead of n/a",
        "    return None if sessions == 0 else Decimal(orders) * 100 / sessions",
        "    return Decimal(0) if sessions == 0 else Decimal(orders) * 100 / sessions",
        "a percent of zero sessions is not a number",
    ),
    (
        "web",
        "the order rate is a fraction, not a percent",
        "    return None if sessions == 0 else Decimal(orders) * 100 / sessions",
        "    return None if sessions == 0 else Decimal(orders) / sessions",
        "20 orders over 410 sessions is 4.9 percent",
    ),
    (
        "web",
        "website totals get no per reported day figure",
        "per_day = _per_day(key, cur, prev, None, WEB_TOTAL_KEYS) if partial",
        "per_day = _per_day(key, cur, prev, None) if partial",
        "a total over 29 days against one over 30 must show the change per reported day",
    ),
    (
        "web",
        "a week is eight days",
        "WEEK_DAYS = 7",
        "WEEK_DAYS = 8",
        "the weeks of a month are day 1 to 7, 8 to 14 and so on",
    ),
    (
        "web",
        "a website nobody synced reads as zero instead of not available",
        "    if not web.has_baseline:\n        none = {key: None for key in WEB_ORDER}",
        "    if False:\n        none = {key: None for key in WEB_ORDER}",
        "no rows and no sync is not a month of zero sessions",
    ),
    (
        "insights",
        "the website figures are left out of the facts",
        "    if report.web is not None:\n        _add_web_facts(facts, report)",
        "    if False:\n        _add_web_facts(facts, report)",
        "the figure check must know every figure the website lines print",
    ),
    (
        "insights",
        "the website total sentence states a direction from the raw totals alone",
        "        if web_states_total_with_per_day(web, key):\n            lines.append(_web_total_sentence(report, key))",
        "        if False:\n            lines.append(_web_total_sentence(report, key))",
        "the narrative must give the per reported day change beside a website total in a partial month",
    ),
    (
        "insights",
        "visitors and users are not words for users per active day",
        '    "Users per active day": ("users per active day", "users", "visitors", "user count"),',
        '    "Users per active day": ("users per active day",),',
        "a synonym for a metric names that metric",
    ),
    (
        "insights",
        "visits is not a word for sessions",
        '    "Sessions": ("sessions", "session count", "visits"),',
        '    "Sessions": ("sessions", "session count"),',
        "a synonym for a metric names that metric",
    ),
    (
        "insights",
        "views is not a word for page views",
        '    "Page views": ("page views", "pageviews", "views"),',
        '    "Page views": ("page views", "pageviews"),',
        "a synonym for a metric names that metric",
    ),
    (
        "deck",
        "the weekly sessions against orders table is dropped",
        "    if web.weeks:\n        _text(slide, MARGIN, Inches(4.1)",
        "    if False:\n        _text(slide, MARGIN, Inches(4.1)",
        "the deck must set sessions against orders",
    ),
    (
        "deck",
        "the order rate note drops how many days it counted",
        '    if row.key == "order_rate" and row.current is not None:',
        "    if False:",
        "a rate over days both sources reported must say how many",
    ),
    (
        "deck",
        "the data health title does not name the source that is missing days",
        "    if cur.missing_days and web.missing_days:",
        "    if False:",
        "a title with two sources must say which one is missing days",
    ),
    (
        "service",
        "the website connection is picked from the stores",
        "where status = 'active' and platform = any($2::text[]) and ($1::uuid is null or id = $1::uuid) ",
        "where status = 'active' and platform <> all($2::text[]) and ($1::uuid is null or id = $1::uuid) ",
        "a store must never be read as the website",
    ),
    (
        "service",
        "two websites are summed instead of refused",
        "    if len(found) > 1:",
        "    if False:",
        "two websites, ad accounts or properties would be added into one doubled total",
    ),
    (
        "service",
        "a website id that is not found is ignored",
        "    if connection_id and not found:",
        "    if False:",
        "a named website, ad account or property that does not exist must be an error, not a silent deck without it",
    ),
    (
        "m006",
        "the metric migration cannot run twice",
        "on conflict (key) do nothing;",
        ";",
        "a migration run twice must not fail",
    ),
    (
        "m006",
        "users are registered as a delta",
        "    ('users',      'Users',      'count', 'gauge'),",
        "    ('users',      'Users',      'count', 'delta'),",
        "a distinct count per day cannot be summed across days",
    ),
    (
        "m007",
        "ad spend is not money in the currency check",
        "metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value')",
        "metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_purchase_value')",
        "an ad amount with no currency, or a currency on ad spend, must be refused by the database",
    ),
    (
        "m007",
        "purchase value is not money in the currency check",
        "metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value')",
        "metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend')",
        "a purchase value is money and needs the ad account's currency beside it",
    ),
    (
        "m007",
        "the old currency check is kept beside the new one",
        "alter table metric_snapshot drop constraint if exists currency_iff_money;",
        "select 1;",
        "the check is replaced, not left standing with the old list",
    ),
    (
        "m007",
        "the ad metric migration cannot run twice",
        "on conflict (key) do nothing;",
        ";",
        "a migration run twice must not fail",
    ),
    (
        "m007",
        "ad spend is registered as a gauge",
        "    ('ad_spend',          'Ad spend',       'minor_currency', 'delta'),",
        "    ('ad_spend',          'Ad spend',       'minor_currency', 'gauge'),",
        "spend on a day is an amount that can be added across days",
    ),
    (
        "m008",
        "a top row may have more clicks than impressions",
        "    constraint clicks_within_impressions check (clicks <= impressions),",
        "    constraint clicks_within_impressions check (clicks >= 0),",
        "a click is an impression that was clicked",
    ),
    (
        "m008",
        "a top row may be stored twice for one day and key",
        "    unique (connection_id, observed_on, dimension, key)",
        "    unique (id)",
        "two rows for one query on one day must be impossible",
    ),
    (
        "m008",
        "a top row may belong to a dimension nobody asked for",
        "check (dimension in ('query', 'page')),",
        "check (dimension in ('query', 'page', 'country')),",
        "only queries and pages are stored",
    ),
    (
        "m008",
        "row level security is not forced on the top rows",
        "alter table search_top_row force row level security;",
        "select 1;",
        "the owner of the table must be filtered like everyone else",
    ),
    (
        "m008",
        "every tenant reads every tenant's top rows",
        "    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)\n    with check (",
        "    using (true)\n    with check (",
        "one tenant's search queries must never reach another's report",
    ),
    (
        "m008",
        "a later sync keeps the old top rows of a day beside the new",
        "     where t.connection_id = d.connection_id and t.observed_on = d.observed_on;",
        "     where t.connection_id = d.connection_id and t.observed_on = d.observed_on and false;",
        "a query that left a day's top list must be gone, not kept beside the new list",
    ),
    (
        "m008",
        "the sync worker may not delete the rows it replaces",
        "grant select, insert, update, delete on search_top_row to sync_worker;",
        "grant select, insert, update on search_top_row to sync_worker;",
        "replacing a day's rows needs delete",
    ),
    (
        "m008",
        "average position is registered as a delta",
        "    ('search_position',    'Average position', 'ratio', 'gauge')",
        "    ('search_position',    'Average position', 'ratio', 'delta')",
        "a position is an average and must never be summed across days",
    ),
    (
        "m009",
        "two requests with one key can both build",
        "    unique (tenant_id, idempotency_key),",
        "    unique (id),",
        "the unique key is what makes two simultaneous requests build one deck",
    ),
    (
        "m009",
        "a report that was not built can be marked delivered",
        "    constraint delivered_only_when_done check (delivered_at is null or status = 'done')",
        "    constraint delivered_only_when_done check (true)",
        "nothing is delivered that was not built",
    ),
    (
        "m009",
        "a finished run can have no deck",
        "    constraint done_has_a_deck check (status <> 'done' or (deck is not null and deck_sha256 is not null and slides is not null)),",
        "    constraint done_has_a_deck check (true),",
        "a done run holds its deck, its hash and its size",
    ),
    (
        "m009",
        "a failed run can have no reason",
        "    constraint failed_has_a_reason check (status <> 'failed' or error is not null),",
        "    constraint failed_has_a_reason check (true),",
        "a failed run says why",
    ),
    (
        "m009",
        "an idempotency key can be anything",
        "check (idempotency_key ~ '^[A-Za-z0-9:_.@-]{8,128}$'),",
        "check (true),",
        "a key with spaces or no length must be refused",
    ),
    (
        "m009",
        "row level security is not forced on the report runs",
        "alter table report_run force row level security;",
        "select 1;",
        "the owner of the table must be filtered like everyone else",
    ),
    (
        "m009",
        "the request role may not update a run",
        "grant select, insert, update on report_run to app_request;",
        "grant select, insert on report_run to app_request;",
        "a run is claimed and then finished, which is an update",
    ),
]


#: Returned instead of a bool when the suite never finished. Distinct from False
#: on purpose: "an assertion failed" and "the code spun forever" are different
#: facts about the mutation, and collapsing them loses the more alarming one.
HUNG_SENTINEL = "hung"

#: The full suite runs in about ten seconds. Three minutes is not slow, it is
#: stuck, and waiting ten minutes for that verdict cost a whole run once.
MUTANT_TIMEOUT_SECONDS = 180
#: The whole suite is far past three minutes now (database tests included), so the green check before and after a run gets its own limit.
BASELINE_TIMEOUT_SECONDS = 1800


#: The change detection guards live in code that nothing else imports, so their mutants run only the suite that can see them (about 30 seconds instead of 60). The baseline and the final
#: check still run everything.
SCOPED_SUITE = {key: "tests/test_change_detection.py" for key in ("m005", "changes", "cli")}
#: The report guards run the report's own test files (space separated), which see all of them in a few seconds.
SCOPED_SUITE.update({key: "tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_service_db.py tests/test_report_cli.py tests/test_report_web.py tests/test_report_web_deck.py tests/test_report_paid.py tests/test_report_search.py tests/test_report_sources_deck.py" for key in ("report", "insights", "deck")})
#: The GA4 connector's guards run its own tests (a real HTTP server on the loopback address, a few seconds) and the registry's.
SCOPED_SUITE["ga4"] = "tests/test_ga4_connector.py tests/test_ga4_fake_server.py tests/test_registry.py"
SCOPED_SUITE["web"] = "tests/test_report_web.py tests/test_report_web_deck.py"
#: The service and migration guards need Postgres: the sync through to the report, and the report's own database tests.
SCOPED_SUITE.update({key: "tests/test_ga4_sync_db.py tests/test_report_service_db.py tests/test_report_sources_db.py" for key in ("service", "m006")})
#: The migrations of the ad, search and report run work each run against the database tests that apply them.
SCOPED_SUITE["m007"] = "tests/test_meta_sync_db.py"
SCOPED_SUITE["m008"] = "tests/test_gsc_sync_db.py"
SCOPED_SUITE["m009"] = "tests/test_report_api_db.py"


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS, path: str = "tests/"):
    try:
        r = subprocess.run(
            [str(PY), "-m", "pytest", *path.split(), "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return HUNG_SENTINEL
    return r.returncode == 0


def baseline_path(only: set[str] | None) -> str:
    """The tests the green checks run: all of them, or only the suites of the selected keys when a run is narrowed with --only."""
    if not only:
        return "tests/"
    suites = [SCOPED_SUITE.get(key, "tests/") for key in sorted(only)]
    return "tests/" if "tests/" in suites else " ".join(dict.fromkeys(" ".join(suites).split()))


def missing_targets() -> list[str]:
    """Guards whose old text is no longer in the file they aim at, or that change nothing. Such a guard proves nothing, and a full run takes about an hour to say so."""
    problems = []
    for key, name, old, new, *_ in MUTATIONS:
        if old not in TARGETS[key].read_text(encoding="utf-8"):
            problems.append(f"{name}: its target text is no longer in {TARGETS[key].name}")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--check-targets", action="store_true",
                        help="only check that every guard finds its target text, edit nothing")
    parser.add_argument("--only", help="comma separated target keys, for a quick loop while writing a guard")
    parser.add_argument("--match", help="comma separated words; only guards whose name holds one run")
    return parser.parse_args(argv)


def _restore_all(snapshot: dict[Path, bytes]) -> None:
    """Put back every file whose bytes differ from the start of the run, byte for byte."""
    for path, data in snapshot.items():
        if path.read_bytes() != data:
            path.write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problems = missing_targets()
    if problems:
        print("update this file before mutating, these guards no longer find their target:\n  " + "\n  ".join(problems))
        return 1
    if args.check_targets:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0

    only = set(args.only.split(",")) if args.only else None
    match = args.match.split(",") if args.match else None
    snapshot = {path: path.read_bytes() for path in set(TARGETS.values())}

    def _terminate(signum, frame):  # a SIGTERM must unwind through the finally block below
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:  # not the main thread, as under a test runner
        pass

    try:
        baseline = baseline_path(only)
        if run_suite(timeout=BASELINE_TIMEOUT_SECONDS, path=baseline) is not True:
            print("baseline suite is RED or hung, fix that before mutating")
            return 1
        print("baseline: green")

        failures = 0
        for key, name, old, new, why in MUTATIONS:
            if only and key not in only:
                continue
            if match and not any(word in name for word in match):
                continue
            path = TARGETS[key]
            original = path.read_text(encoding="utf-8")
            if old not in original:
                print(f"[SKIP] {name}: mutation target not found, update this file")
                failures += 1
                continue
            try:
                path.write_text(original.replace(old, new, 1), encoding="utf-8")
                still_green = run_suite(path=SCOPED_SUITE.get(key, "tests/"))
            finally:
                path.write_bytes(snapshot[path])
            assert path.read_bytes() == snapshot[path], "RESTORE FAILED"

            if still_green is HUNG_SENTINEL:
                # Not green, so the guard is tested; and worse than red, because the
                # mutated code does not terminate at all.
                print(f"[ok]   {name}: suite HUNG, the guard also prevents an infinite retry")
            elif still_green:
                print(f"[FAIL] {name}: suite stayed GREEN, so nothing tests that {why}")
                failures += 1
            else:
                print(f"[ok]   {name}: suite went red as it should")

        print()
        if run_suite(timeout=BASELINE_TIMEOUT_SECONDS, path=baseline) is not True:
            print("suite is RED or hung after restoring, a mutation leaked")
            return 1
        print("all sources restored, suite green again")
        print(f"mutation control: {'PASS' if failures == 0 else str(failures) + ' UNTESTED GUARD(S)'}")
        return 1 if failures else 0
    finally:
        _restore_all(snapshot)


if __name__ == "__main__":
    sys.exit(main())
