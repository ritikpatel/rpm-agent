"""Resolve a dataset location that may be a local directory or an s3:// prefix (EKS pods pull the extract
from the encrypted data bucket at start-up instead of baking confidential data into images)."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import boto3

log = logging.getLogger(__name__)


def fetch_dir(uri: str, dest: str | Path | None = None) -> Path:
    if not uri.startswith("s3://"):
        return Path(uri)
    bucket, _, prefix = uri[5:].partition("/")
    target = Path(dest or tempfile.mkdtemp(prefix="rpm-data-"))
    s3 = boto3.client("s3")
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix.rstrip("/")) + 1:]
            if not rel or rel.endswith("/"):
                continue
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(out))
            count += 1
    log.info("dataset fetched", extra={"uri": uri, "files": count, "dest": str(target)})
    return target
