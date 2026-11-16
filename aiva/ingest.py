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
from datetime import timezone
from typing import Callable

from botocore.exceptions import ClientError

from aiva.pricing import DEFAULT_INPUT_PER_1M, DEFAULT_OUTPUT_PER_1M, estimate_cost

INSERT_BATCH_SIZE = 500  # bounds how many full payloads are ever resident at once

# Errors that mean the run cannot work at all, not that one object is bad.
_FATAL_CODES = {"AccessDenied", "AccessDeniedException", "KMS.AccessDeniedException",
                "ExpiredToken", "InvalidAccessKeyId", "SignatureDoesNotMatch"}


@dataclass(frozen=True)
class Source:
    """What a log source provides. Each adapter module defines one as SOURCE."""
    name: str
    label: str          # shown in the run header
    reading: str        # "Reading <reading> from S3..."
    present_check: str  # preflight check name
    empty_hint: str     # preflight hint when no logs are found yet
    log_prefix: Callable     # (prefix) -> key prefix the logs live under
    list_prefixes: Callable  # (s3, bucket, log_prefix, from_date) -> prefixes to list
    wants_key: Callable      # (key relative to log_prefix) -> bool
    parse: Callable          # (json document, fetch_body) -> Invocations


@dataclass
class IngestResult:
    skipped: list = field(default_factory=list)  # (key, reason) for objects or records not read


def s3_key_prefix(prefix, *parts):
    """Join an S3 key prefix and sub-paths, tolerating empty or slash-padded prefixes.

    Bedrock invocation logging allows an empty key prefix, which writes straight to AWSLogs/.
    """
    segments = [seg.strip("/") for seg in (prefix, *parts)]
    joined = "/".join(seg for seg in segments if seg)
    return f"{joined}/" if joined else ""


def read_to_store(store, src, from_date, to_date=None, session=None,
                  rates=(DEFAULT_INPUT_PER_1M, DEFAULT_OUTPUT_PER_1M)):
    """Read one configured source (config.SourceConfig) into `store`, for
    objects written from `from_date` up to `to_date` (UTC datetimes).

    Records are written in batches as they arrive, never held as one list, so
    peak memory stays bounded by the batch size and worker count, not by total
    invocation count (see store.py's module docstring).

    Objects already in the store are not fetched again, so re-running into
    the same --db resumes an interrupted read.

    One unreadable object or record is skipped and reported in the result;
    an access or credentials error stops the run. `session` overrides the
    source's own credentials (tests pass a fake S3). `rates` is
    (input, output) USD per 1M tokens for the cost estimate.
    """
    session = session or src.session()
    source = src.source
    s3 = session.client("s3", region_name=src.region)

    stored = store.stored_keys()
    wanted = [(key, lm) for key, lm in _list_wanted(s3, source, src.bucket, src.prefix, from_date, to_date)
              if key not in stored]
    result = IngestResult()
    if not wanted:
        return result

    # Each S3 GET is a round trip (~1s+), so objects are fetched by a thread
    # pool, one client per worker thread. Results are consumed in order on the
    # main thread and batched into `store` there, so SQLite never sees
    # concurrent writers.
    _local = threading.local()
    _lock = threading.Lock()  # a boto3 session is not safe to create clients from concurrently

    def _client():
        if not hasattr(_local, "s3"):
            with _lock:
                _local.s3 = session.client("s3", region_name=src.region)
        return _local.s3

    def _fetch(item):
        key, lm = item
        return _read_object(_client(), source, src.bucket, key), key, lm

    buffer = []
    max_workers = min(32, max(4, len(wanted)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for (invocations, skipped), key, lm in pool.map(_fetch, wanted):
            result.skipped += skipped
            lm_iso = lm.astimezone(timezone.utc).isoformat()
            # One object can hold several invocations. The first keeps the
            # bare key, which is what stored_keys() checks on a resume.
            for i, invocation in enumerate(invocations):
                invocation.estimated_cost_usd = estimate_cost(
                    invocation.input_tokens, invocation.output_tokens, invocation.cache_read_tokens,
                    invocation.cache_write_tokens, *rates)
                buffer.append((invocation, key if i == 0 else f"{key}#{i}", lm_iso))
            # Flush only between objects, so an object is never half committed.
            if len(buffer) >= INSERT_BATCH_SIZE:
                store.insert_invocations(buffer)
                buffer = []

    if buffer:
        store.insert_invocations(buffer)
    return result


def _list_wanted(s3, source, bucket, prefix, from_date, to_date=None):
    """(key, last_modified) for every in-window object the source wants, oldest
    first so row ids follow time."""
    paginator = s3.get_paginator("list_objects_v2")
    base = source.log_prefix(prefix)
    wanted = []
    for list_prefix in source.list_prefixes(s3, bucket, base, from_date):
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                # <= so a watermark equal to an object's time doesn't re-read it.
                if obj["LastModified"] <= from_date or (to_date and obj["LastModified"] > to_date):
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

