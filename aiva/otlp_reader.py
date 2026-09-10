"""Read OpenTelemetry (OTLP-JSON) log exports from S3 and normalise into
the same record schema as reader.py's Bedrock log reader.

The OTEL Collector's awss3 exporter (marshaler: otlp_json) writes files
containing the standard OTLP JSON structure: a top-level object with
`resourceLogs` (for log events) or `resourceSpans` (for traces). Each
resource entry carries resource-level attributes (service.name, enduser.id)
and scope-level records with per-event attributes.

This reader handles log events (resourceLogs). Trace spans (resourceSpans)
are supported as a secondary signal source when log events are not available.

Cost uses the source-reported cost_usd attribute when present (e.g. Claude
Code includes this). Otherwise it is estimated from token counts and model
ID using the shared pricing module.
"""

import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3

from aiva.pricing import estimate_cost


INSERT_BATCH_SIZE = 500


def read_otlp_logs_to_store(store, bucket, prefix, region, days, since=None):
    """Read OTLP-JSON files from S3 and write normalised records into `store`.

    Same contract as reader.read_invocation_logs_to_store: records are
    batched into the store as they arrive, peak memory stays bounded.

    Returns newest_iso (max object LastModified, ISO-8601 UTC) or None.
    """
    s3 = boto3.client("s3", region_name=region)

    if since is not None:
        cutoff = _coerce_dt(since)
    else:
        resumed = store.resume_watermark()
        cutoff = _coerce_dt(resumed) if resumed else datetime.now(timezone.utc) - timedelta(days=days)

    paginator = s3.get_paginator("list_objects_v2")

    wanted = []
    list_prefix = prefix if prefix.endswith("/") else f"{prefix}/"
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            last_modified = obj["LastModified"]
            if last_modified <= cutoff:
                continue
            key = obj["Key"]
            if not (key.endswith(".json") or key.endswith(".json.gz") or key.endswith(".gz")):
                continue
            wanted.append((key, last_modified))

    if not wanted:
        return None

    newest = None
    buffer = []

    _local = threading.local()

    def _client():
        if not hasattr(_local, "s3"):
            _local.s3 = boto3.client("s3", region_name=region)
        return _local.s3

    def _fetch(item):
        key, lm = item
        return _read_otlp_file(_client(), bucket, key), key, lm

    max_workers = min(32, max(4, len(wanted)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for records, key, lm in pool.map(_fetch, wanted):
            for i, record in enumerate(records):
                record_key = f"{key}#record-{i}" if len(records) > 1 else key
                buffer.append((record, record_key, lm.astimezone(timezone.utc).isoformat()))
                if newest is None or lm > newest:
                    newest = lm
                if len(buffer) >= INSERT_BATCH_SIZE:
                    store.insert_invocations(buffer)
                    buffer = []

    if buffer:
        store.insert_invocations(buffer)

    return newest.astimezone(timezone.utc).isoformat() if newest else None


def _coerce_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_otlp_file(s3, bucket, key):
    """Read and parse an OTLP-JSON file. Returns a list of normalised records.

    Each file may contain multiple resource entries, each with multiple log
    records, so one S3 object can yield many normalised invocation records.
    """
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()

    if key.endswith(".gz"):
        body = gzip.decompress(body)

    try:
        raw = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return []

    if not isinstance(raw, dict):
        return []

    records = []

    if "resourceLogs" in raw:
        for resource_log in raw["resourceLogs"]:
            resource_attrs = _extract_resource_attrs(resource_log)
            for scope_log in resource_log.get("scopeLogs", []):
                for log_record in scope_log.get("logRecords", []):
                    record = _normalise_log_record(log_record, resource_attrs)
                    if record:
                        records.append(record)

    # Only fall back to spans when no log records were found, to avoid
    # double-counting the same LLM call reported as both a log and a span.
    if not records and "resourceSpans" in raw:
        for resource_span in raw["resourceSpans"]:
            resource_attrs = _extract_resource_attrs(resource_span)
            for scope_span in resource_span.get("scopeSpans", []):
                for span in scope_span.get("spans", []):
                    record = _normalise_span_record(span, resource_attrs)
                    if record:
                        records.append(record)

    return records


def _extract_resource_attrs(resource_entry):
    """Extract resource-level attributes into a flat dict."""
    attrs = {}
    resource = resource_entry.get("resource", {})
    for attr in resource.get("attributes", []):
        key = attr.get("key", "")
        value = _extract_attr_value(attr.get("value", {}))
        if key and value is not None:
            attrs[key] = value
    return attrs


def _extract_attr_value(value_obj):
    """Extract a typed OTLP attribute value to a Python primitive."""
    if not isinstance(value_obj, dict):
        return value_obj
    for vtype in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if vtype in value_obj:
            return value_obj[vtype]
    if "arrayValue" in value_obj:
        return [_extract_attr_value(v) for v in value_obj["arrayValue"].get("values", [])]
    return None


def _extract_log_attrs(log_record):
    """Extract log-record-level attributes into a flat dict."""
    attrs = {}
    for attr in log_record.get("attributes", []):
        key = attr.get("key", "")
        value = _extract_attr_value(attr.get("value", {}))
        if key and value is not None:
            attrs[key] = value
    return attrs


def _normalise_log_record(log_record, resource_attrs):
    """Convert an OTLP log record into our internal invocation format."""
    attrs = _extract_log_attrs(log_record)

    event_name = attrs.get("event.name", attrs.get("name", ""))
    if not _is_relevant_event(event_name, attrs):
        return None

    time_nano = log_record.get("timeUnixNano", log_record.get("observedTimeUnixNano", 0))
    timestamp = _nano_to_iso(time_nano)

    model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = _extract_model_and_tokens(attrs, resource_attrs)
    cost, cost_from_source = _extract_cost(attrs, input_tokens, output_tokens, model, cache_read_tokens, cache_write_tokens)
    caller = _extract_caller(attrs, resource_attrs)
    session_key = (attrs.get("session.id")
                   or resource_attrs.get("session.id")
                   or "")

    body = log_record.get("body", {})
    body_text = ""
    if isinstance(body, dict):
        body_text = body.get("stringValue", "")
    elif isinstance(body, str):
        body_text = body

    system_prompt = attrs.get("gen_ai.system_prompt", "")
    user_prompt = attrs.get("prompt", attrs.get("gen_ai.prompt", ""))
    response_text = attrs.get("response", attrs.get("gen_ai.completion", ""))

    tools = _extract_list_attr(attrs, "tools")
    tool_name = attrs.get("tool.name", attrs.get("tool_name", ""))
    tool_calls = [tool_name] if tool_name else []

    messages = []
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    if not messages and body_text:
        messages.append({"role": "user", "content": body_text})

    service_name = resource_attrs.get("service.name", "")

    return {
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": cost,
        "cost_priced": cost_from_source,
        "system_prompt": system_prompt,
        "tools": tools,
        "messages": messages,
        "response_text": response_text,
        "tool_calls": tool_calls,
        "max_tokens": _to_int(attrs.get("gen_ai.request.max_tokens")) or None,
        "caller": caller,
        "user_agent": service_name,
        "operation": event_name or "otlp_log",
        "request_id": attrs.get("request_id", attrs.get("gen_ai.request.id", "")),
        "metadata": _build_metadata(attrs, resource_attrs, session_key),
    }


def _normalise_span_record(span, resource_attrs):
    """Convert an OTLP trace span into our internal invocation format.

    Spans carry structured timing and nesting but the same attribute
    conventions as log records for gen_ai.* fields.
    """
    attrs = {}
    for attr in span.get("attributes", []):
        key = attr.get("key", "")
        value = _extract_attr_value(attr.get("value", {}))
        if key and value is not None:
            attrs[key] = value

    span_name = span.get("name", "")
    if not _is_relevant_span(span_name, attrs):
        return None

    time_nano = span.get("startTimeUnixNano", 0)
    timestamp = _nano_to_iso(time_nano)

    model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = _extract_model_and_tokens(attrs, resource_attrs)
    cost, cost_from_source = _extract_cost(attrs, input_tokens, output_tokens, model, cache_read_tokens, cache_write_tokens)
    caller = _extract_caller(attrs, resource_attrs)
    session_key = (attrs.get("session.id")
                   or resource_attrs.get("session.id")
                   or "")

    system_prompt = attrs.get("gen_ai.system_prompt", "")
    user_prompt = attrs.get("prompt", attrs.get("gen_ai.prompt", ""))
    response_text = attrs.get("response", attrs.get("gen_ai.completion", ""))

    tools = _extract_list_attr(attrs, "tools")
    tool_name = attrs.get("tool.name", attrs.get("tool_name", ""))
    tool_calls = [tool_name] if tool_name else []

    messages = []
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})

    service_name = resource_attrs.get("service.name", "")

    return {
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": cost,
        "cost_priced": cost_from_source,
        "system_prompt": system_prompt,
        "tools": tools,
        "messages": messages,
        "response_text": response_text,
        "tool_calls": tool_calls,
        "max_tokens": _to_int(attrs.get("gen_ai.request.max_tokens")) or None,
        "caller": caller,
        "user_agent": service_name,
        "operation": span_name or "otlp_span",
        "request_id": attrs.get("request_id", attrs.get("gen_ai.request.id", "")),
        "metadata": _build_metadata(attrs, resource_attrs, session_key),
    }


def _extract_model_and_tokens(attrs, resource_attrs):
    """Extract model ID and token counts from attributes, checking both
    Claude Code's native names and gen_ai.* convention names."""
    model = (attrs.get("model")
             or attrs.get("gen_ai.request.model")
             or attrs.get("gen_ai.response.model")
             or resource_attrs.get("gen_ai.request.model")
             or "unknown")
    input_tokens = _to_int(attrs.get("input_tokens",
                                      attrs.get("gen_ai.usage.input_tokens", 0)))
    output_tokens = _to_int(attrs.get("output_tokens",
                                       attrs.get("gen_ai.usage.output_tokens", 0)))
    cache_read_tokens = _to_int(attrs.get("cache_read_tokens",
                                           attrs.get("gen_ai.usage.cache_read_input_tokens", 0)))
    cache_write_tokens = _to_int(attrs.get("cache_creation_tokens",
                                            attrs.get("gen_ai.usage.cache_creation_input_tokens", 0)))
    return model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens


def _extract_cost(attrs, input_tokens, output_tokens, model,
                  cache_read_tokens, cache_write_tokens):
    """Extract cost, preferring source-reported cost_usd when available."""
    source_cost = attrs.get("cost_usd")
    if source_cost is not None:
        try:
            return float(source_cost), True
        except (ValueError, TypeError):
            pass
    cost, _ = estimate_cost(input_tokens, output_tokens, model,
                            cache_read_tokens, cache_write_tokens)
    return cost, False


def _extract_caller(attrs, resource_attrs):
    """Extract caller identity, checking Claude Code's user.email first."""
    return (attrs.get("user.email")
            or attrs.get("user.id")
            or resource_attrs.get("enduser.id")
            or resource_attrs.get("user.email")
            or "unknown")


def _build_metadata(attrs, resource_attrs, session_key):
    """Build the metadata dict that store.py uses for session_key extraction."""
    metadata = {}
    if session_key:
        metadata["session_id"] = session_key
    metadata["source_format"] = "otlp"
    metadata["service_name"] = resource_attrs.get("service.name", "")
    if resource_attrs.get("service.version"):
        metadata["service_version"] = resource_attrs["service.version"]
    if attrs.get("deployment.environment"):
        metadata["environment"] = attrs["deployment.environment"]
    if resource_attrs.get("deployment.environment"):
        metadata["environment"] = resource_attrs["deployment.environment"]
    return metadata


def _is_relevant_event(event_name, attrs):
    """Filter to events that represent LLM invocations or tool calls worth classifying."""
    relevant_names = (
        "api_request", "user_prompt", "assistant_response", "tool_result",
        "tool_decision", "api_error",
        "claude_code.user_prompt", "claude_code.tool_result",
        "claude_code.api_request", "claude_code.api_error",
    )
    relevant_prefixes = (
        "claude_code.", "gen_ai.", "llm.",
    )

    if event_name in relevant_names:
        return True
    if any(event_name.startswith(p) for p in relevant_prefixes):
        return True

    if attrs.get("gen_ai.usage.input_tokens") or attrs.get("gen_ai.request.model"):
        return True
    if attrs.get("input_tokens") and attrs.get("model"):
        return True

    return False


def _is_relevant_span(span_name, attrs):
    """Filter to spans that represent LLM calls or meaningful tool invocations."""
    relevant_names = (
        "api_request", "user_prompt", "assistant_response", "tool_result",
    )
    relevant_prefixes = (
        "claude_code.", "gen_ai.", "llm.",
    )
    if span_name in relevant_names:
        return True
    if any(span_name.startswith(p) for p in relevant_prefixes):
        return True
    if attrs.get("gen_ai.usage.input_tokens") or attrs.get("gen_ai.request.model"):
        return True
    if attrs.get("input_tokens") and attrs.get("model"):
        return True
    return False


def _nano_to_iso(nano):
    """Convert nanosecond Unix timestamp to ISO-8601 string."""
    if not nano:
        return ""
    try:
        ts = int(nano) / 1_000_000_000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ""


def _to_int(value):
    """Safely coerce to int, returning 0 on failure."""
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _extract_list_attr(attrs, key):
    """Extract a list-valued attribute, or return empty list."""
    val = attrs.get(key, [])
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        return [val]
    return []
