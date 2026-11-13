"""Read log objects from S3 into the store, for any source in aiva.sources.

Everything here is source-agnostic: listing, fetching, gzip, splitting an
object into JSON documents, skipping bad objects, resume and batching. A
source only says which keys to list and how to turn one JSON document into
Invocations, so a fix to any of this applies to every source at once.
"""

import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

INSERT_BATCH_SIZE = 500  # bounds how many full payloads are ever resident at once

# Errors that mean the run cannot work at all, not that one object is bad.
_FATAL_CODES = {"AccessDenied", "AccessDeniedException", "KMS.AccessDeniedException",
                "ExpiredToken", "InvalidAccessKeyId", "SignatureDoesNotMatch"}


@dataclass
class IngestResult:
    newest: str | None = None  # max LastModified read (ISO-8601 UTC), the next run's watermark
    objects: int = 0
    invocations: int = 0
    skipped: list = field(default_factory=list)  # (key, reason) for objects or records not read


def s3_key_prefix(prefix, *parts):
    """Join an S3 key prefix and sub-paths, tolerating empty or slash-padded prefixes.

    Bedrock invocation logging allows an empty key prefix, which writes straight to AWSLogs/.
    """
    segments = [seg.strip("/") for seg in (prefix, *parts)]
    joined = "/".join(seg for seg in segments if seg)
    return f"{joined}/" if joined else ""


def read_to_store(store, source, bucket, prefix, region, days, since=None):
    """Read `source` logs from S3 and write Invocations into `store`.

    Records are written in batches as they arrive, never held as one list, so
    peak memory stays bounded by the batch size and worker count, not by total
    invocation count (see store.py's module docstring).

    Cutoff precedence, most specific wins: an explicit `since` (ISO-8601 str
    or datetime) > `store.resume_watermark()` (a prior run into this same --db
    already has data, so this call is a resume) > `days`. Objects are fetched
    and committed in LastModified order, so the watermark read back from the
    store is a true high-water mark even when a central bucket holds several
    accounts and regions. INSERT OR IGNORE on s3_key makes the re-listed
    overlap a no-op.

    One unreadable object or record is skipped and reported in the result;
    an access or credentials error stops the run.
    """
    s3 = boto3.client("s3", region_name=region)

    if since is not None:
        cutoff = _coerce_dt(since)
    else:
        resumed = store.resume_watermark()
        cutoff = _coerce_dt(resumed) if resumed else datetime.now(timezone.utc) - timedelta(days=days)

    wanted = _list_wanted(s3, source, bucket, prefix, cutoff)
    result = IngestResult(objects=len(wanted))
    if not wanted:
        return result

    # Each S3 GET is a round trip (~1s+), so objects are fetched by a thread
    # pool, one client per worker thread. Results are consumed in order on the
    # main thread and batched into `store` there, so SQLite never sees
    # concurrent writers.
    _local = threading.local()

    def _client():
        if not hasattr(_local, "s3"):
            _local.s3 = boto3.client("s3", region_name=region)
        return _local.s3

    def _fetch(item):
        key, lm = item
        return _read_object(_client(), source, bucket, key), key, lm

    newest = None
    buffer = []
    max_workers = min(32, max(4, len(wanted)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for (invocations, skipped), key, lm in pool.map(_fetch, wanted):
            result.skipped += skipped
            if not invocations:
                continue
            lm_iso = lm.astimezone(timezone.utc).isoformat()
            # One object can hold several invocations. The first keeps the
            # bare key so rows from older stores still dedupe.
            for i, invocation in enumerate(invocations):
                buffer.append((invocation, key if i == 0 else f"{key}#{i}", lm_iso))
            result.invocations += len(invocations)
            if newest is None or lm > newest:
                newest = lm
            if len(buffer) >= INSERT_BATCH_SIZE:
                store.insert_invocations(buffer)
                buffer = []

    if buffer:
        store.insert_invocations(buffer)

    result.newest = newest.astimezone(timezone.utc).isoformat() if newest else None
    return result


def _list_wanted(s3, source, bucket, prefix, cutoff):
    """(key, last_modified) for every in-window object the source wants, oldest first."""
    paginator = s3.get_paginator("list_objects_v2")
    base = source.log_prefix(prefix)
    wanted = []
    for list_prefix in source.list_prefixes(s3, bucket, base, cutoff):
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                # <= so a watermark equal to an object's time doesn't re-read it.
                if obj["LastModified"] <= cutoff:
                    continue
                # Only the part after the prefix is matched, so a prefix such
                # as "platform/data/" cannot filter out every key.
                if source.wants_key(obj["Key"][len(base):]):
                    wanted.append((obj["Key"], obj["LastModified"]))
    wanted.sort(key=lambda item: item[1])
    return wanted


def _read_object(s3, source, bucket, key):
    """Return (invocations, skipped) for one object. Never raises for a bad
    object or record, only for errors that would fail every object."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if key.endswith(".gz"):
            body = gzip.decompress(body)
        text = body.decode("utf-8")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _FATAL_CODES:
            raise RuntimeError(
                f"Cannot read s3://{bucket}/{key}: {code}. Check s3:GetObject on the bucket and "
                "kms:Decrypt on its key (see SourceBucketKmsKeyArn in the CloudFormation template)."
            ) from e
        return [], [(key, code or str(e))]
    except (OSError, EOFError, UnicodeDecodeError) as e:
        return [], [(key, type(e).__name__)]

    def _fetch_body(s3_uri):
        return _fetch_json(s3, s3_uri)

    invocations, skipped = [], []
    for doc in iter_json_documents(text):
        for item in doc if isinstance(doc, list) else [doc]:
            try:
                invocations.extend(source.parse(item, _fetch_body))
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                skipped.append((key, f"record: {type(e).__name__}"))
    return invocations, skipped


def iter_json_documents(text):
    """Yield each JSON document in `text`, stopping at the first malformed one.

    Model Invocation Logging batches several invocations into one object as
    concatenated JSON documents, and OTLP file exporters write one export per
    line, so a single json.loads fails on both.
    """
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return
        try:
            doc, i = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return
        yield doc


def _fetch_json(s3, s3_uri):
    """Fetch a JSON body stored as a separate S3 object (s3://bucket/key), or {}."""
    if not isinstance(s3_uri, str) or not s3_uri.startswith("s3://"):
        return {}
    parts = s3_uri[5:].split("/", 1)
    if len(parts) != 2:
        return {}
    ref_bucket, ref_key = parts
    try:
        body = s3.get_object(Bucket=ref_bucket, Key=ref_key)["Body"].read()
        if ref_key.endswith(".gz"):
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def _coerce_dt(value):
    """Accept an ISO-8601 string or datetime, return a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
