# LIFESPAN: permanent
"""Wait until the S3 stand-in (SeaweedFS) accepts a WRITE, then exit 0; exit 1 if it never does within 90 seconds.

CI starts SeaweedFS with `docker run` and used to wait only for its gateway to answer 403 on `/`. A freshly started SeaweedFS answers there before its volume server can place an object, so the first
PutObject calls came back as InternalError and four archive tests failed (2026-11-17, run 43983474415). A real write is the only proof the store is ready, so this makes one, with retries.
Run from the repo root: python tests/s3_warmup.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # the repo root, so `app` imports when this is run as a file (pytest gets it from its rootdir, a plain `python tests/s3_warmup.py` does not)
sys.path.insert(0, HERE)
from jobs_fixtures import make_bucket  # noqa: E402

DEADLINE_S = 90


def main() -> int:
    end = time.time() + DEADLINE_S
    last = None
    while time.time() < end:
        try:
            s3 = make_bucket("warmup")
            s3.put_object(Bucket="warmup", Key="ping", Body=b"ok")
            print("S3 stand-in accepts writes")
            return 0
        except Exception as exc:  # the store is still starting: try again
            last = exc
            time.sleep(2)
    print("S3 stand-in never accepted a write within %d s: %s" % (DEADLINE_S, str(last)[:200]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
