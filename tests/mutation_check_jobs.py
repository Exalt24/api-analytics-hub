# LIFESPAN: permanent (the repo's own mutation check for the job pipeline, run by hand)
"""Mutation control for the job pipeline: break each guard one at a time and prove the tests for it turn red.

It works on a COPY of the repository (app, tests, migrations, n8n), made in a temporary folder and deleted at the end, so a killed run leaves nothing weakened in
the real tree. Each mutant is one exact text replacement in one file; the named test files then run in the copy, and the mutant is KILLED when they fail. A mutant
whose text is not found in the file is an error, not a pass, so a refactor that moves a guard cannot quietly turn this into a check of nothing.

    python tests/mutation_check_jobs.py --check-targets       confirm every guard finds its text, run nothing
    python tests/mutation_check_jobs.py --match tailor,hitl   only the mutants whose name holds a word
    python tests/mutation_check_jobs.py                       every mutant

The database suites need `docker compose --profile jobs --profile s3 up -d --wait` and drop the `public` schema of ANALYTICS_TEST_DSN, like every database suite here.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_DIRS = ("app", "tests", "migrations", "n8n")
T_TAILOR, T_FORM, T_PIPE, T_HITL, T_DEDUPE, T_JS, T_NODE = (
    "tests/test_jobs_tailor.py", "tests/test_jobs_formfill.py", "tests/test_jobs_pipeline_db.py", "tests/test_jobs_hitl_db.py", "tests/test_jobs_dedupe.py",
    "tests/test_jobs_vendors_js.py", "node:n8n/jobs/normalize.test.js")

# (name, file, old text, new text, test files, why it matters)
MUTATIONS = [
    ("tailor: the technology list is ignored", "app/jobs/tailor.py", "low in TERMS or any(c.isdigit()", "False or any(c.isdigit()", [T_TAILOR], "a lower case invented tool must be caught"),
    ("tailor: numbers in the text are not checked against the facts", "app/jobs/tailor.py", 'if n.rstrip(".") not in facts_numbers and n.rstrip(".") not in vocab:', "if False:", [T_TAILOR], "an invented figure"),
    ("tailor: a round with an invented claim is accepted", "app/jobs/tailor.py", "        if bad:\n            feedback", "        if False:\n            feedback", [T_TAILOR], "an ungrounded document must never leave the loop"),
    ("tailor: the score ceiling counts tools the facts lack", "app/jobs/tailor.py", "supported = [t for t in terms if t in have]", "supported = terms", [T_TAILOR], "a job that asks for tools the person lacks must read as unreachable"),
    ("tailor: the cover letter is not grounded", "app/jobs/tailor.py", '+ ungrounded(doc["cover_letter"], profile, "%s %s" % (job.get("company_name") or "", job.get("title") or ""))', "", [T_TAILOR], "a letter can invent too"),
    ("tailor: the model is skipped when the facts already pass", "app/jobs/tailor.py", "    if llm is None:\n        return", "    if llm is None or best[\"score\"] >= threshold:\n        return", [T_TAILOR], "the cover letter only the model writes is still wanted"),
    ("formfill: a field is typed under the threshold", "app/jobs/formfill.py", "ok = m.value is not None and m.confidence >= threshold", "ok = m.value is not None", [T_FORM], "the 0.95 rule"),
    ("formfill: consent boxes are no longer left to a person", "app/jobs/formfill.py", "if f.kind == \"checkbox\" or CONSENT.search(f.label):", "if False:", [T_FORM], "an agreement is a person's"),
    ("formfill: self identification is answered by a rule", "app/jobs/formfill.py", "if SELF_ID.search(f.label):", "if False:", [T_FORM], "never guess a demographic answer"),
    ("formfill: a page with no readable form is submitted", "app/jobs/formfill.py", "if step == 1 and not fields:", "if False:", [T_FORM], "an empty application is worse than none"),
    ("formfill: any page counts as success", "app/jobs/formfill.py", "return bool(SUCCESS.search(await page.inner_text(\"body\")))", "return True", [T_FORM], "a rejected submission must read as failed"),
    ("formfill: a label about someone else can be the applicant's name", "app/jobs/formfill.py", "if key in PERSONAL and NOT_ME & set(label.split()):", "if False:", [T_FORM], "company name is not the applicant's name"),
    ("formfill: a short phrase inside a longer one is a rival", "app/jobs/formfill.py", "if not any(k2 != k and p != p2 and re.search", "if not any(False and re.search", [T_FORM], "Legal last name must not be ambiguous with name"),
    ("pipeline: invalid jobs are not counted", "app/jobs/pipeline.py", "res.invalid = res.received - len(good)", "res.invalid = 0", [T_PIPE], "a mapping fault must show as a number"),
    ("pipeline: the window ignores the vendor", "app/jobs/pipeline.py", 'members = ["%s:%s" % (fingerprint(j["dedupe_key"]), vendor) for j in good]', 'members = [fingerprint(j["dedupe_key"]) for j in good]', [T_PIPE], "a second vendor must reach the upsert"),
    ("pipeline: every sighting is embedded", "app/jobs/pipeline.py", "                        if inserted:\n                            inserted_rows.append", "                        if True:\n                            inserted_rows.append", [T_PIPE], "a repeat sighting costs no embedding call"),
    ("pipeline: upserts take their locks in arrival order and a deadlock is not retried", "app/jobs/pipeline.py",
     ['ordered = sorted(fresh, key=lambda j: fingerprint(j["dedupe_key"]))', "DEADLOCK_TRIES = 4"], ["ordered = list(fresh)", "DEADLOCK_TRIES = 1"], [T_PIPE], "parallel vendors sharing jobs must not deadlock"),
    ("notion: a 429 is not waited out", "app/jobs/notion.py", "            if r.status_code == 429:\n                if attempt + 1 == self._attempts:", "            if False:\n                if attempt + 1 == self._attempts:",
     ["tests/test_jobs_notion_db.py"], "a rate limited board must be retried by its Retry-After"),
    ("hitl: a board that refuses stops the queue", "app/jobs/hitl.py", "                except Exception as exc:\n                    log.warning(\"the Notion board refused a review page", "                except ZeroDivisionError as exc:\n                    log.warning(\"the Notion board refused a review page",
     ["tests/test_jobs_notion_db.py"], "the hub's own table is the queue, never the board"),
    ("pipeline: a finished batch is processed again", "app/jobs/pipeline.py", 'if prior and prior["finished_at"] is not None and prior["status"] != "partial":', "if False:", [T_PIPE],
     "a retried delivery must be answered from the first run"),
    ("browser: a proxy that failed is handed out again", "app/jobs/browser.py", "            if self._bad_until.get(p, 0.0) <= now:", "            if True:", ["tests/test_jobs_sessions.py"], "a failed proxy sits out its cool-down"),
    ("browser: the loopback bypass is left on, so a local page skips the proxy", "app/jobs/browser.py", '"--proxy-bypass-list=<-loopback>"] if proxy', '"--proxy-bypass-list=<loopback>"] if proxy', ["tests/test_jobs_sessions.py"],
     "a proxy test must use the proxy"),
    ("formfill: paced typing is ignored", "app/jobs/formfill.py", "    elif typing_delay:", "    elif False:", ["tests/test_jobs_sessions.py"], "typing at a person's pace makes a key event for each character"),
    ("formfill: the form inventory differs from the one Puppeteer runs", "app/jobs/inventory.js", "const label = labelOf(el);", "const label = '';", ["tests/test_jobs_formfill.py", "tests/test_jobs_puppeteer.py"], "one inventory file, two drivers"),
    ("formfill: a radio group is named by its own Yes label and not the legend", "app/jobs/inventory.js", "label: groupLabel(el),", "label: labelOf(el),", ["tests/test_jobs_formfill.py"], "the question is the legend; Yes is only an option"),
    ("store: a backwards salary range is kept", "app/jobs/store.py", "        lo, hi = hi, lo", "        pass", [T_PIPE], "salary_min must not exceed salary_max"),
    ("hitl: a stale resume token resumes the run", "app/jobs/hitl.py", "where id=$1::uuid and resume_token=$2 and state='waiting_review' returning id", "where id=$1::uuid and state='waiting_review' returning id", [T_HITL], "the one-time token"),
    ("hitl: a task can be answered twice", "app/jobs/hitl.py", "where id=$1::uuid and status='pending' returning run_id::text, field_label, notion_page_id", "where id=$1::uuid returning run_id::text, field_label, notion_page_id", [T_HITL], "a double click must not write twice"),
    ("hitl: the run resumes before the last answer", "app/jobs/hitl.py", "ready = left == 0 and token is not None", "ready = token is not None", [T_HITL], "all open questions first"),
    ("hitl: a failing message is never dead lettered", "app/jobs/hitl.py", "if attempts + 1 >= MAX_ATTEMPTS:", "if False:", [T_HITL], "a poison message must end somewhere"),
    ("normalise: the key uses the web address when there is one", "n8n/jobs/normalize.js", "const who = norm(company).replace(/ (inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|the)$/g, '').trim() || companyDomain || '';", "const who = companyDomain || norm(company);", [T_JS, T_NODE], "the same job from two vendors must be one key"),
    ("normalise: a record with no title is valid", "n8n/jobs/normalize.js", "valid: Boolean(title && (applyUrl || company)),", "valid: Boolean(applyUrl || company),", [T_NODE], "never store a job with no title"),
    ("normalise: a lookalike host is an ATS", "n8n/jobs/normalize.js", "['lever', /jobs\\.(?:eu\\.)?lever\\.co\\/", "['lever', /lever\\.co\\/", [T_NODE], "notlever.co is not Lever"),
    ("pull: a 429 is not waited out", "n8n/jobs/pull.js", "if (res.status === 429 || res.status >= 500) {", "if (false) {", [T_JS], "a throttled vendor must be retried"),
    ("pull: paging never stops on a short page", "n8n/jobs/pull.js", "if (rows.length < (cfg.pageSize || 25)) break;", "", [T_JS], "the page count is part of the proof"),
    ("pull: the Bright Data snapshot is read before it is ready", "n8n/jobs/pull.js", "if (status === 'ready') {", "if (status === 'ready' || status === 'running') {", [T_JS], "poll until ready"),
]


def run_tests(copy: Path, tests: list[str], timeout: int) -> tuple[int, str]:
    out = []
    for t in tests:
        if t.startswith("node:"):
            cmd = ["node", "--test", t[5:]]
        else:
            cmd = [sys.executable, "-m", "pytest", t, "-q", "-x", "-p", "no:warnings"]
        p = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out.append(p.stdout[-300:])
        if p.returncode != 0:
            return p.returncode, "\n".join(out)
    return 0, "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-targets", action="store_true")
    ap.add_argument("--match", default="")
    args = ap.parse_args()
    words = [w for w in args.match.split(",") if w]
    chosen = [m for m in MUTATIONS if not words or any(w in m[0] for w in words)]
    bad = [m[0] for m in chosen if any(o not in (ROOT / m[1]).read_text(encoding="utf-8").replace("\r\n", "\n") for o in (m[2] if isinstance(m[2], list) else [m[2]]))]
    if bad:
        print("these guards did not find their text, the file changed: %s" % bad)
        return 2
    if args.check_targets:
        print("every one of the %d guards finds its text" % len(chosen))
        return 0
    copy = Path(tempfile.mkdtemp(prefix="jobsmut-"))
    survived: list[str] = []
    try:
        for d in COPY_DIRS:
            shutil.copytree(ROOT / d, copy / d, ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc", ".venv", "test-runs"))
        for f in ("requirements.txt", "compose.yaml"):
            shutil.copyfile(ROOT / f, copy / f)
        code, out = run_tests(copy, sorted({t for m in chosen for t in m[4]}), 900)
        if code != 0:
            print("the unmutated copy does not pass, nothing to compare against:\n%s" % out)
            return 3
        for name, rel, old, new, tests, why in chosen:
            target = copy / rel
            original = target.read_bytes()
            text = original.decode("utf-8").replace("\r\n", "\n")
            for o, n in zip(old if isinstance(old, list) else [old], new if isinstance(new, list) else [new]):
                text = text.replace(o, n, 1)
            target.write_bytes(text.encode("utf-8"))
            started = time.monotonic()
            try:
                code, out = run_tests(copy, tests, 600)
            except subprocess.TimeoutExpired:
                code, out = 1, "timed out"
            finally:
                target.write_bytes(original)
            status = "KILLED " if code != 0 else "SURVIVED"
            print("%s %-62s %5.1fs  (%s)" % (status, name, time.monotonic() - started, why))
            if code == 0:
                survived.append(name)
        print("\n%d mutants, %d killed, %d survived" % (len(chosen), len(chosen) - len(survived), len(survived)))
        for s in survived:
            print("  SURVIVED: %s" % s)
        return 1 if survived else 0
    finally:
        shutil.rmtree(copy, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
