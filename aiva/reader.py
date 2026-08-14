import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3


# Per-1K-token on-demand prices (USD), sourced from the model catalog
# ($/1M ÷ 1000). Keyed by the canonical model family token found in the
# model ID. Longest matching key wins so opus-4-8 never collides with a
# shorter opus prefix. Cache read/write are priced as ratios of the input
# rate (Bedrock/Anthropic 5-minute-TTL convention): read ~0.1x, write ~1.25x.
#
# NOTE: on-demand estimate only. Provisioned throughput, batch (-50%), and
# 1-hour cache TTL (2x write) are NOT modelled - see _get_pricing.
PRICING_PER_1K = {
    "claude-opus-4-8": {"input": 0.005, "output": 0.025},
    "claude-opus-4-7": {"input": 0.005, "output": 0.025},
    "claude-opus-4-6": {"input": 0.005, "output": 0.025},
    "claude-opus-4-5": {"input": 0.005, "output": 0.025},
    "claude-sonnet-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005},
    "claude-haiku-3-5": {"input": 0.0008, "output": 0.004},
    "claude-haiku-3": {"input": 0.00025, "output": 0.00125},
    "nova-pro": {"input": 0.0008, "output": 0.0032},
    "nova-lite": {"input": 0.00006, "output": 0.00024},
    "nova-micro": {"input": 0.000035, "output": 0.00014},
    "titan-text-express": {"input": 0.0002, "output": 0.0006},
}

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# Fallback when a model ID matches no known family. Chosen to be obviously
# wrong-looking (0) so unpriced models surface as $0.00 rather than silently
# borrowing another tier's rate. _normalise_record flags these.
UNKNOWN_PRICING = {"input": 0.0, "output": 0.0}


INSERT_BATCH_SIZE = 500  # bounds how many full payloads are ever resident at once


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
    log_prefix = f"{prefix}/AWSLogs/"

    # 1. List phase: collect in-window log keys (cheap, listing only).
    wanted = []  # (key, last_modified)
    for page in paginator.paginate(Bucket=bucket, Prefix=log_prefix):
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
        for record, key, lm in pool.map(_fetch, wanted):
            if record:
                buffer.append((record, key, lm.astimezone(timezone.utc).isoformat()))
                if newest is None or lm > newest:
                    newest = lm
                if len(buffer) >= INSERT_BATCH_SIZE:
                    store.insert_invocations(buffer)
                    buffer = []

    if buffer:
        store.insert_invocations(buffer)

    return newest.astimezone(timezone.utc).isoformat() if newest else None


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
    """Read and parse a single log file. Each file is one JSON record (not JSONL)."""
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()

    if key.endswith(".gz"):
        body = gzip.decompress(body)

    try:
        raw = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None

    # Handle case where file contains a list (shouldn't happen but defensive)
    if isinstance(raw, list):
        if raw:
            raw = raw[0]
        else:
            return None

    return _normalise_record(raw, s3, bucket)


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

    pricing, priced = _get_pricing(model_id)
    cost = (input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"]
    # Cache read is cheaper than fresh input; cache write costs a premium.
    cost += (cache_read_tokens / 1000) * pricing["input"] * CACHE_READ_MULTIPLIER
    cost += (cache_write_tokens / 1000) * pricing["input"] * CACHE_WRITE_MULTIPLIER

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


def _extract_response_text(output_body):
    """Extract response text from output body (handles streaming event arrays)."""
    if isinstance(output_body, dict):
        # Non-streaming response
        content = output_body.get("content", [])
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return " ".join(parts)
        return ""

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
        content = output_body.get("content", [])
        if isinstance(content, list):
            return [block.get("name", "unknown") for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"]
        return []

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


def _get_pricing(model_id):
    """Look up per-1K-token pricing by longest matching family token.

    Returns (pricing, priced) where priced is False when the model ID
    matched no known family (so callers can flag the cost as unreliable).
    Longest-match avoids opus-4-8 colliding with a shorter opus key.
    """
    model_lower = model_id.lower()

    matches = [key for key in PRICING_PER_1K if key in model_lower]
    if matches:
        best = max(matches, key=len)
        return PRICING_PER_1K[best], True

    return UNKNOWN_PRICING, False
