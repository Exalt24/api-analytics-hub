"""Raw vendor JSON, gzipped, in S3 compatible object storage: Cloudflare R2, AWS S3, or the SeaweedFS stand-in the tests use.

The batch is written BEFORE anything is normalised or stored, so a mapping bug found next week can be fixed and the batch replayed from
exactly what the vendor sent. The key is derived from the tenant, vendor, date and batch id, so writing the same batch twice writes the
same object and a retried webhook cannot leave two copies. The object's SHA-256 is kept as metadata and checked on read.

R2 speaks the S3 API with region `auto` and path style addressing; for AWS leave `endpoint_url` empty. Credentials come from the
environment (JOBS_S3_*), never from a file in the repository.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def client_from_env(env: dict[str, str] | None = None):
    e = env if env is not None else os.environ
    return boto3.client(
        "s3",
        endpoint_url=e.get("JOBS_S3_ENDPOINT") or None,
        aws_access_key_id=e.get("JOBS_S3_ACCESS_KEY"),
        aws_secret_access_key=e.get("JOBS_S3_SECRET_KEY"),
        region_name=e.get("JOBS_S3_REGION") or "auto",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20),
    )


class ArchiveError(RuntimeError):
    pass


class RawArchive:
    def __init__(self, s3, bucket: str):
        self._s3 = s3
        self._bucket = bucket

    @staticmethod
    def key_for(tenant_id: str, vendor: str, batch_id: str, when: datetime | None = None) -> str:
        d = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return "raw/%s/%s/%04d/%02d/%02d/%s.json.gz" % (_SAFE.sub("-", tenant_id), _SAFE.sub("-", vendor), d.year, d.month, d.day, _SAFE.sub("-", batch_id))

    def put_batch(self, tenant_id: str, vendor: str, batch_id: str, records: Any, *, when: datetime | None = None) -> str:
        body = gzip.compress(json.dumps(records, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8"), mtime=0)
        key = self.key_for(tenant_id, vendor, batch_id, when)
        digest = hashlib.sha256(body).hexdigest()
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json", ContentEncoding="gzip", Metadata={"sha256": digest})
        except (ClientError, OSError) as exc:
            raise ArchiveError("could not write %s: %s" % (key, exc)) from exc
        return key

    def get_batch(self, key: str) -> Any:
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            body = obj["Body"].read()
        except (ClientError, OSError) as exc:
            raise ArchiveError("could not read %s: %s" % (key, exc)) from exc
        want = (obj.get("Metadata") or {}).get("sha256")
        if want and hashlib.sha256(body).hexdigest() != want:
            raise ArchiveError("%s does not match its stored checksum" % key)
        return json.loads(gzip.decompress(body))

    def put_file(self, key: str, data: bytes, content_type: str) -> str:
        """A generated document (the tailored PDF or DOCX) under its own key."""
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, Metadata={"sha256": hashlib.sha256(data).hexdigest()})
        except (ClientError, OSError) as exc:
            raise ArchiveError("could not write %s: %s" % (key, exc)) from exc
        return key
