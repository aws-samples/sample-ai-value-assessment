"""Bedrock Model Invocation Logs adapter: which keys to read and how one log
record becomes an Invocation. S3, threads and the store live in aiva.ingest."""

from datetime import datetime, timedelta, timezone

from aiva.ingest import s3_key_prefix
from aiva.invocation import Invocation, as_int
from aiva.pricing import estimate_cost


MAX_PARTITION_DAYS = 400  # day folders listed at most, for a very old cutoff


def log_prefix(prefix):
    return s3_key_prefix(prefix, "AWSLogs")


def wants_key(relative_key):
    """Log objects only. Large bodies under /data/ and permission-check files are not records."""
    if "permission-check" in relative_key or "/data/" in relative_key:
        return False
    return relative_key.endswith(".json.gz") or relative_key.endswith(".json")


def parse(raw, fetch_body):
    """Convert one Model Invocation Log record into Invocations (zero or one).

    `fetch_body(s3_uri)` returns a body Bedrock stored as a separate object.
    """
    if not isinstance(raw, dict):
        return []

    model_id = raw.get("modelId", "unknown")
    input_section = raw.get("input") or {}
    output_section = raw.get("output") or {}

    input_tokens = as_int(input_section.get("inputTokenCount"))
    output_tokens = as_int(output_section.get("outputTokenCount"))
    cache_read_tokens = as_int(input_section.get("cacheReadInputTokenCount"))
    cache_write_tokens = as_int(input_section.get("cacheWriteInputTokenCount"))

    cost, priced = estimate_cost(input_tokens, output_tokens, model_id,
                                  cache_read_tokens, cache_write_tokens)

    # Get input body (inline or from S3 reference)
    input_body = input_section.get("inputBodyJson", {})
    if not input_body and "inputBodyS3Path" in input_section:
        input_body = fetch_body(input_section["inputBodyS3Path"])

    output_body = output_section.get("outputBodyJson", {})
    identity = raw.get("identity")

    return [Invocation(
        timestamp=raw.get("timestamp", ""),
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        estimated_cost_usd=cost,
        cost_priced=priced,
        system_prompt=_extract_system_prompt(input_body),
        tools=_extract_tools(input_body),
        messages=_extract_messages(input_body),
        response_text=_extract_response_text(output_body),
        max_tokens=_extract_max_tokens(input_body),
        caller=identity.get("arn", "unknown") if isinstance(identity, dict) else "unknown",
        user_agent=raw.get("userAgent", ""),
        operation=raw.get("operation", "unknown"),
        request_id=raw.get("requestId", ""),
        metadata=raw.get("requestMetadata", {}),
    )]


def list_prefixes(s3, bucket, base, cutoff):
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
    for account_prefix in _subdirs(base):
        region_prefixes += _subdirs(f"{account_prefix}BedrockModelInvocationLogs/")
    if not region_prefixes:
        return [base]

    today = datetime.now(timezone.utc).date()
    day = max(cutoff.date() - timedelta(days=1), today - timedelta(days=MAX_PARTITION_DAYS))
    days = []
    while day <= today:
        days.append(day)
        day += timedelta(days=1)
    return [f"{rp}{d:%Y/%m/%d}/" for rp in region_prefixes for d in days]


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
