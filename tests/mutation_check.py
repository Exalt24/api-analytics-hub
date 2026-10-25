"""Mutation control: break each load-bearing guard and prove the suite goes red.

A green suite is not evidence. On a previous project 48 passing tests proved
nothing about whether the service could boot, because the tests and the code
shared the same wrong assumption. The only way to know a guard is tested is to
delete it and watch something fail.

Each mutation below targets a decision that is SILENTLY wrong when removed: no
crash, no error, just a number that is off in a way nobody notices until a client
queries their own revenue.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"

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
        'return ("Recovered. " + describe([]) if old else None), {"keys": [], "alerted_at": 0.0}',
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
        "    select upper(carrier_clean_text(raw))",
        "    select carrier_clean_text(raw)",
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
        "the stricter whole number grounding check is dropped",
        "        if _normalise(raw) not in allowed and raw not in bad:\n            bad.append(raw)",
        "        pass",
        "a number hiding inside a longer fact must still be refused",
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
        '    notes = ["Missing days in %s: %s." % (cur.name, days_text(cur.missing_days))]',
        "    notes = []",
        "the days a reader cannot trust must be named on the slide",
    ),
]


#: Returned instead of a bool when the suite never finished. Distinct from False
#: on purpose: "an assertion failed" and "the code spun forever" are different
#: facts about the mutation, and collapsing them loses the more alarming one.
HUNG_SENTINEL = "hung"

#: The full suite runs in about ten seconds. Three minutes is not slow, it is
#: stuck, and waiting ten minutes for that verdict cost a whole run once.
MUTANT_TIMEOUT_SECONDS = 180


#: The change detection guards live in code that nothing else imports, so their mutants run only the suite that can see them (about 30 seconds instead of 60). The baseline and the final
#: check still run everything.
SCOPED_SUITE = {key: "tests/test_change_detection.py" for key in ("m005", "changes", "cli")}
#: The report guards run the report's own test files (space separated), which see all of them in a few seconds.
SCOPED_SUITE.update({key: "tests/test_monthly_report.py tests/test_report_insights.py tests/test_report_deck.py tests/test_report_service_db.py tests/test_report_cli.py" for key in ("report", "insights", "deck")})


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS, path: str = "tests/"):
    try:
        r = subprocess.run(
            [str(PY), "-m", "pytest", *path.split(), "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return HUNG_SENTINEL
    return r.returncode == 0


def missing_targets() -> list[str]:
    """Guards whose old text is no longer in the file they aim at, or that change nothing. Such a guard proves nothing, and a full run takes about an hour to say so."""
    problems = []
    for key, name, old, new, *_ in MUTATIONS:
        if old not in TARGETS[key].read_text(encoding="utf-8"):
            problems.append(f"{name}: its target text is no longer in {TARGETS[key].name}")
        elif old == new:
            problems.append(f"{name}: the mutation changes nothing")
    return problems


def main() -> int:
    problems = missing_targets()
    if problems:
        print("update this file before mutating, these guards no longer find their target:\n  " + "\n  ".join(problems))
        return 1
    if "--check-targets" in sys.argv:
        print(f"all {len(MUTATIONS)} guards find their target")
        return 0
    if run_suite() is not True:
        print("baseline suite is RED or hung, fix that before mutating")
        return 1
    print("baseline: green")

    # `--only m005,changes` runs just the guards aimed at those targets, for a quick loop while writing a new one. A full run is the one that counts.
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None

    # `--match absent,previous` runs only the guards whose name holds one of those words.
    match = sys.argv[sys.argv.index("--match") + 1].split(",") if "--match" in sys.argv else None

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
            path.write_text(original, encoding="utf-8")
            assert path.read_text(encoding="utf-8") == original, "RESTORE FAILED"

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
    if run_suite() is not True:
        print("suite is RED or hung after restoring, a mutation leaked")
        return 1
    print("all sources restored, suite green again")
    print(f"mutation control: {'PASS' if failures == 0 else str(failures) + ' UNTESTED GUARD(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
