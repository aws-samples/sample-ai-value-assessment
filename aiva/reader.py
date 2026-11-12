import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3

from aiva.pricing import estimate_cost


INSERT_BATCH_SIZE = 500  # bounds how many full payloads are ever resident at once
MAX_PARTITION_DAYS = 400  # day folders listed at most, for a very old cutoff


def s3_key_prefix(prefix, *parts):
    """Join an S3 key prefix and sub-paths, tolerating empty or slash-padded prefixes.

    Bedrock invocation logging allows an empty key prefix, which writes straight to AWSLogs/.
    """
    segments = [seg.strip("/") for seg in (prefix, *parts)]
    joined = "/".join(seg for seg in segments if seg)
    return f"{joined}/" if joined else ""


def read_invocation_logs_to_store(store, bucket, prefix, region, days, since=None):
    """Read Model Invocation Logs from S3 and write normalised records into `store`.

    Does not return a list of invocations, that is the whole point: at org
    scale (hundreds of thousands to millions of invocations) holding every
    invocation's full payload in one Python list is the actual memory
    ceiling (see docs/ARCHITECTURE.md's memory-ceiling section), not a speed
    problem. Records are fetched concurrently, same thread-pool pattern as
    before, but written to `store` in small batches (INSERT_BATCH_SIZE) as
    they arrive rather than accumulated, so peak memory stays bounded by the
    batch size and worker count, not by total invocation count.

    Cutoff precedence, most specific wins: an explicit `since` (ISO-8601 str
    or datetime) > `store.resume_watermark()` (non-empty store: a prior run
    into this same --db already has data, so this call is a resume, not a
    fresh read) > `days` (a fresh read with no prior state). A long read
    (a real 30GB window is an estimated 5-8 hours) killed partway through and
    re-run against the same --db picks up near where it left off instead of
    re-listing and re-fetching everything; INSERT OR IGNORE on s3_key makes
    the small re-listed overlap a safe no-op, see store.py's module docstring.

    Returns newest_iso, the max object LastModified seen (ISO-8601, UTC) or
    None if nothing matched, for the caller to persist as the next run's
    watermark.
    """
    s3 = boto3.client("s3", region_name=region)

    if since is not None:
        cutoff = _coerce_dt(since)
    else:
        resumed = store.resume_watermark()
        cutoff = _coerce_dt(resumed) if resumed else datetime.now(timezone.utc) - timedelta(days=days)

    paginator = s3.get_paginator("list_objects_v2")
    log_prefix = s3_key_prefix(prefix, "AWSLogs")

    # 1. List phase: collect in-window log keys (cheap, listing only).
    wanted = []  # (key, last_modified)
    for list_prefix in _day_prefixes(s3, bucket, log_prefix, cutoff):
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                last_modified = obj["LastModified"]
                # <= so a watermark equal to an object's time doesn't re-read it.
                if last_modified <= cutoff:
                    continue
                key = obj["Key"]
                # Skip permission-check files and data files (bodies stored separately).
                if "permission-check" in key or "/data/" in key:
                    continue
                if not key.endswith(".json.gz") and not key.endswith(".json"):
                    continue
                wanted.append((key, last_modified))

    if not wanted:
        return None

    # 2. Fetch phase: download + parse concurrently. Each S3 GET is a round trip
    # (~1s+); serial fetching made a real bucket take tens of minutes. A thread
    # pool cuts it to ~1 min. One S3 client per worker thread (thread-local),
    # not one per object. Results are consumed on the main thread only, and
    # batched into `store` there, so SQLite never sees concurrent writers.
    newest = None
    buffer = []

    _local = threading.local()

    def _client():
        if not hasattr(_local, "s3"):
            _local.s3 = boto3.client("s3", region_name=region)
        return _local.s3

    def _fetch(item):
        key, lm = item
        return _read_log_file(_client(), bucket, key), key, lm

    max_workers = min(32, max(4, len(wanted)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for records, key, lm in pool.map(_fetch, wanted):
            if records:
                lm_iso = lm.astimezone(timezone.utc).isoformat()
                # One object can hold several invocations. The first keeps the
                # bare key so rows from older stores still dedupe.
                for i, record in enumerate(records):
                    buffer.append((record, key if i == 0 else f"{key}#{i}", lm_iso))
                if newest is None or lm > newest:
                    newest = lm
                if len(buffer) >= INSERT_BATCH_SIZE:
                    store.insert_invocations(buffer)
                    buffer = []

    if buffer:
        store.insert_invocations(buffer)

    return newest.astimezone(timezone.utc).isoformat() if newest else None


def _day_prefixes(s3, bucket, log_prefix, cutoff):
    """S3 prefixes to list for objects modified after `cutoff`.

    Model Invocation Logging writes keys as
    AWSLogs/{account}/BedrockModelInvocationLogs/{region}/YYYY/MM/DD/HH/...
    Listing only the day folders from the cutoff onward keeps each poll
    proportional to new data, not to the whole bucket. One extra day before
    the cutoff covers objects written just after midnight. Falls back to the
    whole log prefix when the bucket does not follow that layout.
    """
    def _subdirs(prefix):
        pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
        return [cp["Prefix"] for page in pages for cp in page.get("CommonPrefixes", [])]

    region_prefixes = []
    for account_prefix in _subdirs(log_prefix):
        region_prefixes += _subdirs(f"{account_prefix}BedrockModelInvocationLogs/")
    if not region_prefixes:
        return [log_prefix]

    today = datetime.now(timezone.utc).date()
    day = max(cutoff.date() - timedelta(days=1), today - timedelta(days=MAX_PARTITION_DAYS))
    days = []
    while day <= today:
        days.append(day)
        day += timedelta(days=1)
    return [f"{rp}{d:%Y/%m/%d}/" for rp in region_prefixes for d in days]


def _coerce_dt(value):
    """Accept an ISO-8601 string or datetime, return a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_log_file(s3, bucket, key):
    """Read and parse one log object into a list of normalised records.

    Model Invocation Logging batches several invocations into one object as
    concatenated JSON documents (newline-separated), so a single json.loads
    fails on most busy-hour files. Parse document by document instead.
    """
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()

    if key.endswith(".gz"):
        body = gzip.decompress(body)

    records = []
    for raw in _iter_json_documents(body.decode("utf-8")):
        for item in raw if isinstance(raw, list) else [raw]:
            record = _normalise_record(item, s3, bucket)
            if record:
                records.append(record)
    return records


def _iter_json_documents(text):
    """Yield each JSON document in `text`, stopping at the first malformed one."""
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


def _normalise_record(raw, s3, bucket):
    """Convert a raw Model Invocation Log entry into our internal format."""
    if not isinstance(raw, dict):
        return None

    model_id = raw.get("modelId", "unknown")
    input_section = raw.get("input", {})
    output_section = raw.get("output", {})

    input_tokens = input_section.get("inputTokenCount", 0)
    output_tokens = output_section.get("outputTokenCount", 0)
    cache_read_tokens = input_section.get("cacheReadInputTokenCount", 0)
    cache_write_tokens = input_section.get("cacheWriteInputTokenCount", 0)

    cost, priced = estimate_cost(input_tokens, output_tokens, model_id,
                                  cache_read_tokens, cache_write_tokens)

    # Get input body (inline or from S3 reference)
    input_body = input_section.get("inputBodyJson", {})
    if not input_body and "inputBodyS3Path" in input_section:
        input_body = _fetch_s3_body(s3, input_section["inputBodyS3Path"])

    # Get output body
    output_body = output_section.get("outputBodyJson", {})

    system_prompt = _extract_system_prompt(input_body)
    tools = _extract_tools(input_body)
    messages = _extract_messages(input_body)
    response_text = _extract_response_text(output_body)
    tool_calls = _extract_tool_calls(output_body)
    max_tokens = _extract_max_tokens(input_body)

    return {
        "timestamp": raw.get("timestamp", ""),
        "model": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": cost,
        "cost_priced": priced,
        "system_prompt": system_prompt,
        "tools": tools,
        "messages": messages,
        "response_text": response_text,
        "tool_calls": tool_calls,
        "max_tokens": max_tokens,
        "caller": raw.get("identity", {}).get("arn", "unknown") if isinstance(raw.get("identity"), dict) else "unknown",
        "user_agent": raw.get("userAgent", ""),
        "operation": raw.get("operation", "unknown"),
        "request_id": raw.get("requestId", ""),
        "metadata": raw.get("requestMetadata", {}),
    }


def _fetch_s3_body(s3, s3_path):
    """Fetch a large input/output body stored as a separate S3 object."""
    if not s3_path.startswith("s3://"):
        return {}

    parts = s3_path[5:].split("/", 1)
    if len(parts) != 2:
        return {}

    ref_bucket, ref_key = parts
    try:
        response = s3.get_object(Bucket=ref_bucket, Key=ref_key)
        body = response["Body"].read()
        if ref_key.endswith(".gz"):
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def _extract_system_prompt(input_body):
    """Extract system prompt from various Bedrock request formats."""
    if not isinstance(input_body, dict):
        return ""

    # Anthropic Messages API format (most common via InvokeModel)
    if "system" in input_body:
        system = input_body["system"]
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            parts = []
            for block in system:
                if isinstance(block, dict):
                    if "text" in block:
                        parts.append(block["text"])
            return " ".join(parts)

    # Converse API format
    if "systemPrompt" in input_body:
        return input_body["systemPrompt"]

    return ""


def _extract_tools(input_body):
    """Extract tool names from the request."""
    if not isinstance(input_body, dict):
        return []

    # Anthropic Messages API
    if "tools" in input_body and isinstance(input_body["tools"], list):
        return [t.get("name", "unknown") for t in input_body["tools"] if isinstance(t, dict)]

    # Converse API
    if "toolConfig" in input_body:
        tools = input_body.get("toolConfig", {}).get("tools", [])
        return [t.get("toolSpec", {}).get("name", "unknown") for t in tools if isinstance(t, dict) and "toolSpec" in t]

    return []


def _extract_max_tokens(input_body):
    """Extract the max output-token cap from the request, if any (else None)."""
    if not isinstance(input_body, dict):
        return None
    # Anthropic Messages API
    if "max_tokens" in input_body:
        return input_body.get("max_tokens")
    # Converse API
    if "inferenceConfig" in input_body and isinstance(input_body["inferenceConfig"], dict):
        return input_body["inferenceConfig"].get("maxTokens")
    return None


def _extract_messages(input_body):
    """Extract user messages, filtering system-reminder injections."""
    if not isinstance(input_body, dict):
        return []

    messages = input_body.get("messages", [])
    if not isinstance(messages, list):
        return []

    # Return all messages but mark which are real user content
    # The classifier will filter system-reminders later
    # Just return a reasonable window
    return messages[-10:] if len(messages) > 10 else messages


def _response_blocks(output_body):
    """Content blocks of a non-streaming response: Anthropic `content`, or Converse `output.message.content`."""
    content = output_body.get("content")
    if content is None:
        output = output_body.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _extract_response_text(output_body):
    """Extract response text from output body (handles streaming event arrays)."""
    if isinstance(output_body, dict):
        # Non-streaming response. Anthropic blocks are typed; Converse blocks are
        # keyed by kind ({"text": ...}, {"toolUse": ...}) with no "type".
        return " ".join(block["text"] for block in _response_blocks(output_body)
                        if block.get("type", "text") == "text" and isinstance(block.get("text"), str))

    if isinstance(output_body, list):
        # Streaming response: array of SSE events
        parts = []
        for event in output_body:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
        return "".join(parts)

    return ""


def _extract_tool_calls(output_body):
    """Extract INVOKED tool calls from the response (name of each tool_use block).

    Distinct from `_extract_tools` (input_body), which lists tools the model
    merely had AVAILABLE for this call. A model can be given 12 tools and use
    none, or use the same one repeatedly - only this reflects what actually
    ran. Streaming responses (SSE event arrays) assemble tool_use content
    incrementally across content_block_start/content_block_delta events, so
    tool names there come from content_block_start, not delta.
    """
    if isinstance(output_body, dict):
        names = []
        for block in _response_blocks(output_body):
            if block.get("type") == "tool_use":
                names.append(block.get("name", "unknown"))
            elif isinstance(block.get("toolUse"), dict):
                names.append(block["toolUse"].get("name", "unknown"))
        return names

    if isinstance(output_body, list):
        names = []
        for event in output_body:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "content_block_start":
                block = event.get("content_block", {})
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    names.append(block.get("name", "unknown"))
        return names

    return []


