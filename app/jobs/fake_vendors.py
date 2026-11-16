"""Stand-in job data vendors on one 127.0.0.1 server: TheirStack, Hirebase, Crustdata, CoreSignal, JobDataAPI, LinkdAPI, PredictLeads, Bright Data and Apify.

Each answers on the path, with the auth header, the paging and the response envelope its own documentation page gives (docs/job-vendors.md lists the
page for each, read on the date shown there). The paths do not collide, so one server and one base URL serve all nine. They are written from the
documentation, not recorded from the services, so they show that the pipeline handles the documented shapes, never how a vendor behaves under load.

THE DATA. `JOBS` is one list of distinct jobs. Every vendor serves a slice of it in its own record shape, and the slices overlap on purpose, so the
same posting arrives from two or three vendors with different ids and field names, which is what the dedupe is for. `expected_distinct()` is the
number of distinct jobs the slices add up to.

FAULTS. `server.faults` (from fake_http) scripts a 429 or a dropped connection for the next request. A wrong key is 401 on every vendor. What each
invents, listed in the sources page as UNVERIFIED: cursor values, snapshot ids, the number of progress polls before `ready`, and error body wording.
"""
from __future__ import annotations

import json
from typing import Any

from app.connectors.fake_http import Answer, FakeHTTP, Seen

ATS_URLS = [
    "https://job-boards.greenhouse.io/{org}/jobs/{n}",
    "https://jobs.lever.co/{org}/5a3b6c7d-1111-4222-8333-{n:012d}",
    "https://{org}.wd5.myworkdayjobs.com/en-US/External/job/Remote/Engineer_JR-{n}",
    "https://jobs.ashbyhq.com/{org}/0c1d2e3f-aaaa-4bbb-8ccc-{n:012d}",
    "https://acme-{org}.example.test/careers/{n}",
]
ORGS = ["acme", "globex", "initech", "umbrella", "hooli", "stark"]
TITLES = ["Data Engineer", "Backend Engineer", "Platform Engineer", "ML Engineer", "Automation Engineer"]


def make_jobs(n: int = 30) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        org = ORGS[i % len(ORGS)]
        url = ATS_URLS[i % len(ATS_URLS)].format(org=org, n=4000000 + i)
        out.append({"i": i, "title": "%s %d" % (TITLES[i % len(TITLES)], i), "company": org.title(), "domain": org + ".com", "url": url,
                    "location": "Remote" if i % 2 else "Manila", "posted": "2026-11-%02dT08:00:00Z" % (1 + i % 16), "desc": "Python and PostgreSQL work at %s." % org.title()})
    return out


JOBS = make_jobs()
#: which job indexes each vendor serves: overlapping slices
SLICES = {
    "theirstack": range(0, 14), "hirebase": range(8, 22), "crustdata": range(16, 28), "coresignal": range(0, 8), "jobdataapi": range(4, 16),
    "linkdapi": range(20, 30), "predictleads": range(10, 18), "techmap": range(12, 20), "brightdata": range(24, 30), "apify": range(2, 9),
}


def expected_distinct(vendors: list[str] | None = None) -> int:
    return len({i for v in (vendors or SLICES) for i in SLICES[v]})


def _ts(j): return {"id": 9000 + j["i"], "job_title": j["title"], "company": j["company"], "company_domain": j["domain"], "location": j["location"], "remote": j["location"] == "Remote",
                    "url": "https://www.linkedin.com/jobs/view/%d" % (7000 + j["i"]), "final_url": j["url"], "date_posted": j["posted"][:10], "description": "<p>%s</p>" % j["desc"],
                    "min_annual_salary_usd": 60000, "max_annual_salary_usd": 90000, "technology_slugs": ["python", "postgresql"]}
def _hb(j): return {"_id": "hb%d" % j["i"], "job_title": j["title"], "company_name": j["company"], "location_raw": j["location"], "location_type": "Remote" if j["location"] == "Remote" else "On-site",
                    "application_link": j["url"], "date_posted": j["posted"][:10], "description": j["desc"], "job_type": "Full-time", "salary_range": {"min": 60000, "max": 90000, "currency": "USD", "period": "yearly"}}
def _cd(j): return {"crustdata_job_id": 5000 + j["i"], "job_details": {"title": j["title"], "url": j["url"]}, "company": {"basic_info": {"name": j["company"]}},
                    "location": {"raw": j["location"]}, "metadata": {"date_added": j["posted"][:19]}}
def _cs(j): return {"id": 3000 + j["i"], "job_id": 3000 + j["i"], "title": j["title"], "location": j["location"], "description": j["desc"], "external_url": j["url"], "company_name": j["company"],
                    "company_url": "http://www.%s" % j["domain"], "created_at": j["posted"][:23], "employment_type": ["Full-time"]}
def _jd(j): return {"id": 1000 + j["i"], "company": {"name": j["company"], "website_url": "https://www.%s" % j["domain"]}, "title": j["title"], "location": j["location"], "has_remote": j["location"] == "Remote",
                    "published": j["posted"][:10], "description": j["desc"], "application_url": j["url"], "salary_min": "60000", "salary_max": "90000", "salary_currency": "USD"}
def _ld(j): return {"jobId": str(2000 + j["i"]), "title": j["title"], "company": {"name": j["company"]}, "location": j["location"], "applyUrl": j["url"]}
def _pl(j): return {"id": "pl-%d" % j["i"], "type": "job_opening", "attributes": {"title": j["title"], "url": j["url"], "first_seen_at": j["posted"], "location": j["location"], "description": j["desc"]},
                    "relationships": {"company": {"data": {"id": "c-" + j["domain"], "type": "company"}}}}
def _tm(j): return {"title": j["title"], "company": j["company"], "city": "Manila" if j["location"] != "Remote" else "Austin", "countryCode": "ph" if j["location"] != "Remote" else "us",
                    "dateCreated": j["posted"], "workPlace": ["Remote"] if j["location"] == "Remote" else ["Onsite"], "workType": ["FullTime"], "skills": ["Python", "PostgreSQL"], "isDirect": True,
                    "externalApplyUrl": j["url"], "jsonLD": {"identifier": "tm%d" % j["i"], "description": j["desc"], "url": j["url"], "sameAs": "https://www.indeed.com/viewjob?jk=tm%d" % j["i"], "datePosted": j["posted"][:10]}}
def _bd(j): return {"job_posting_id": str(8000 + j["i"]), "job_title": j["title"], "company_name": j["company"], "job_location": j["location"], "url": "https://www.linkedin.com/jobs/view/%d" % (8000 + j["i"]),
                    "apply_link": j["url"], "job_posted_date": j["posted"], "job_summary": j["desc"], "job_employment_type": "Full-time"}
def _ap(j): return {"id": "ap%d" % j["i"], "positionName": j["title"], "company": j["company"], "companyWebsite": j["domain"], "location": j["location"], "url": j["url"], "postedAt": j["posted"], "description": j["desc"]}


SHAPES = {"theirstack": _ts, "hirebase": _hb, "crustdata": _cd, "coresignal": _cs, "jobdataapi": _jd, "linkdapi": _ld, "predictleads": _pl, "techmap": _tm, "brightdata": _bd, "apify": _ap}
KEYS = {"theirstack": "ts-key", "hirebase": "hb-key", "crustdata": "cd-key", "coresignal": "cs-key", "jobdataapi": "jd-key", "linkdapi": "ld-key",
        "predictleads": ("pl-key", "pl-token"), "techmap": "tm-key", "brightdata": "bd-key", "apify": "ap-key"}


def _slice(v: str) -> list[dict[str, Any]]:
    return [SHAPES[v](JOBS[i]) for i in SLICES[v]]


class FakeVendors(FakeHTTP):
    def __init__(self, *, keys: dict[str, Any] | None = None, progress_polls: int = 2) -> None:
        super().__init__()
        self.keys = {**KEYS, **(keys or {})}
        self.progress_polls = progress_polls
        self._snapshots: dict[str, int] = {}
        self.pulls: dict[str, int] = {}

    def _bad(self, status: int, msg: str) -> Answer:
        return status, {}, {"error": msg, "message": msg}

    def answer(self, seen: Seen) -> Answer:
        p, h, q = seen.path, seen.headers, seen.query
        body = json.loads(seen.body) if seen.body else {}
        count = lambda v: self.pulls.__setitem__(v, self.pulls.get(v, 0) + 1)  # noqa: E731

        if p == "/v1/jobs/search":  # TheirStack
            if h.get("authorization") != "Bearer %s" % self.keys["theirstack"]:
                return self._bad(401, "invalid token")
            if not any(k in body for k in ("posted_at_max_age_days", "posted_at_gte", "posted_at_lte", "job_id_or", "company_id_or", "company_domain_or", "company_name_or")):
                return self._bad(422, "at least one of the posted_at_* or company filters is required")
            count("theirstack")
            lim, page = int(body.get("limit", 25)), int(body.get("page", 0))
            rows = _slice("theirstack")[page * lim:(page + 1) * lim]
            return 200, {}, {"data": rows, "metadata": {"total_results": len(SLICES["theirstack"]), "total_companies": 0, "truncated_results": 0, "truncated_companies": 0}}

        if p == "/v2/jobs/search":  # Hirebase
            if h.get("x-api-key") != self.keys["hirebase"]:
                return self._bad(401, "Unauthorized")
            count("hirebase")
            lim, page = int(body.get("limit", 10)), int(body.get("page", 1))
            if lim > 100:
                return self._bad(422, "limit must be 100 or less")
            return 200, {}, {"jobs": _slice("hirebase")[(page - 1) * lim:page * lim]}

        if p == "/job/search":  # Crustdata
            if h.get("authorization") != "Bearer %s" % self.keys["crustdata"]:
                return self._bad(401, "Unauthorized")
            if h.get("x-api-version") != "2025-11-01":
                return self._bad(400, "x-api-version header is required")
            count("crustdata")
            lim = int(body.get("limit", 20))
            start = int(body["cursor"].split("-")[1]) if body.get("cursor") else 0
            rows = _slice("crustdata")
            nxt = start + lim
            return 200, {}, {"job_listings": rows[start:nxt], "next_cursor": ("cur-%d" % nxt) if nxt < len(rows) else None, "total_count": len(rows)}

        if p == "/cdapi/v2/job_multi_source/search/es_dsl":  # CoreSignal: ids, and the next page in a header
            if h.get("apikey") != self.keys["coresignal"]:
                return self._bad(401, "Unauthorized")
            count("coresignal")
            ids = [3000 + i for i in SLICES["coresignal"]]
            after = int(q["after"]) if "after" in q else 0
            page = [i for i in ids if i > after][:5]
            hdr = {"x-total-results": str(len(ids))}
            if page and page[-1] != ids[-1]:
                hdr["x-next-page-after"] = str(page[-1])
            return 200, hdr, page
        if p.startswith("/cdapi/v2/job_multi_source/collect/"):
            if h.get("apikey") != self.keys["coresignal"]:
                return self._bad(401, "Unauthorized")
            want = int(p.rsplit("/", 1)[1])
            return 200, {}, _cs(JOBS[want - 3000])

        if p == "/api/jobs/":  # JobDataAPI
            if h.get("authorization") != "Api-Key %s" % self.keys["jobdataapi"]:
                return self._bad(401, "Invalid API key")
            count("jobdataapi")
            size, page = int(q.get("page_size", 200)), int(q.get("page", 1))
            rows = _slice("jobdataapi")
            more = page * size < len(rows)
            return 200, {}, {"count": len(rows), "next": ("http://%s/api/jobs/?page=%d&page_size=%d" % (h.get("host", "127.0.0.1"), page + 1, size)) if more else None, "previous": None,
                             "results": rows[(page - 1) * size:page * size]}

        if p == "/api/v1/jobs/search":  # LinkdAPI
            if h.get("x-linkdapi-apikey") != self.keys["linkdapi"]:
                return self._bad(401, "invalid api key")
            count("linkdapi")
            start = int(q.get("start", 0))
            return 200, {}, {"success": True, "data": {"jobs": _slice("linkdapi")[start:start + 25]}}

        if p.startswith("/api/v3/companies/") and p.endswith("/job_openings"):  # PredictLeads
            k, t = self.keys["predictleads"]
            if h.get("x-api-key") != k or h.get("x-api-token") != t:
                return self._bad(401, "Unauthorized")
            count("predictleads")
            domain = p.split("/")[4]
            rows = [r for r in _slice("predictleads") if r["relationships"]["company"]["data"]["id"] == "c-" + domain]
            incl = [{"id": "c-" + domain, "type": "company", "attributes": {"company_name": domain.split(".")[0].title(), "domain": domain}}] if rows else []
            return 200, {}, {"data": rows, "included": incl, "links": {"next": None}}

        if p == "/api/v2/jobs/search":  # Techmap through RapidAPI
            if h.get("x-rapidapi-key") != self.keys["techmap"] or not h.get("x-rapidapi-host"):
                return self._bad(401, "Invalid RapidAPI key or missing host")
            count("techmap")
            rows = _slice("techmap")
            if q.get("cursor") not in ("true", None) and not str(q.get("cursor", "")).startswith("cur"):
                return self._bad(400, "cursor must be true or a nextCursor")
            start = int(q["cursor"].split("-")[1]) if str(q.get("cursor", "")).startswith("cur-") else 0
            step = 3
            nxt = start + step
            return 200, {"x-ratelimit-requests-remaining": "999"}, {"result": rows[start:nxt], "totalCount": len(rows), **({"nextCursor": "cur-%d" % nxt} if nxt < len(rows) else {})}

        if p == "/datasets/v3/trigger":  # Bright Data
            if h.get("authorization") != "Bearer %s" % self.keys["brightdata"]:
                return self._bad(401, "Unauthorized")
            if not q.get("dataset_id"):
                return self._bad(400, "dataset_id is required")
            count("brightdata")
            sid = "sd_%04d" % (len(self._snapshots) + 1)
            self._snapshots[sid] = 0
            return 200, {}, {"snapshot_id": sid}
        if p.startswith("/datasets/v3/progress/"):
            sid = p.rsplit("/", 1)[1]
            if sid not in self._snapshots:
                return self._bad(404, "snapshot not found")
            self._snapshots[sid] += 1
            return 200, {}, {"status": "ready" if self._snapshots[sid] > self.progress_polls else "running", "snapshot_id": sid}
        if p.startswith("/datasets/v3/snapshot/"):
            sid = p.rsplit("/", 1)[1]
            if sid not in self._snapshots or self._snapshots[sid] <= self.progress_polls:
                return self._bad(404 if sid not in self._snapshots else 409, "snapshot is not ready")
            return 200, {}, _slice("brightdata")

        if p.startswith("/v2/acts/") and p.endswith("/run-sync-get-dataset-items"):  # Apify
            if h.get("authorization") != "Bearer %s" % self.keys["apify"]:
                return self._bad(401, "User was not found or authentication token is not valid")
            count("apify")
            return 201, {}, _slice("apify")

        return 404, {}, {"error": "not found: %s" % p}
