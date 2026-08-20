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
    "crypto": ROOT / "app" / "crypto.py",
    "auth": ROOT / "app" / "api" / "auth.py",
    "apimain": ROOT / "app" / "api" / "main.py",
    "db": ROOT / "app" / "db.py",
    "shopify": ROOT / "app" / "connectors" / "shopify.py",
    "base": ROOT / "app" / "connectors" / "base.py",
    "tokens": ROOT / "app" / "connectors" / "tokens.py",
    "webhooks": ROOT / "app" / "connectors" / "shopify_webhooks.py",
    "registry": ROOT / "app" / "connectors" / "registry.py",
}

MUTATIONS = [
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
]


#: Returned instead of a bool when the suite never finished. Distinct from False
#: on purpose: "an assertion failed" and "the code spun forever" are different
#: facts about the mutation, and collapsing them loses the more alarming one.
HUNG_SENTINEL = "hung"

#: The full suite runs in about ten seconds. Three minutes is not slow, it is
#: stuck, and waiting ten minutes for that verdict cost a whole run once.
MUTANT_TIMEOUT_SECONDS = 180


def run_suite(timeout: int = MUTANT_TIMEOUT_SECONDS):
    try:
        r = subprocess.run(
            [str(PY), "-m", "pytest", "tests/", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return HUNG_SENTINEL
    return r.returncode == 0


def main() -> int:
    if run_suite() is not True:
        print("baseline suite is RED or hung, fix that before mutating")
        return 1
    print("baseline: green")

    failures = 0
    for key, name, old, new, why in MUTATIONS:
        path = TARGETS[key]
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"[SKIP] {name}: mutation target not found, update this file")
            failures += 1
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            still_green = run_suite()
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
