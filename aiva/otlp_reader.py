"""Read OpenTelemetry (OTLP-JSON) log exports from S3 and normalise into
Invocations. S3, threads and the store live in aiva.ingest.

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

from datetime import datetime, timezone

from aiva.ingest import s3_key_prefix
from aiva.invocation import Invocation, as_int
from aiva.pricing import estimate_cost


def log_prefix(prefix):
    return s3_key_prefix(prefix)


def list_prefixes(s3, bucket, base, cutoff):
    return [base]


def wants_key(relative_key):
    return relative_key.endswith((".json", ".gz", ".jsonl", ".ndjson"))


def parse(raw, fetch_body):
    """Convert one OTLP-JSON export document into Invocations.

    One document may contain multiple resource entries, each with multiple
    log records, so it can yield many Invocations.
    """
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

    messages = []
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    if not messages and body_text:
        messages.append({"role": "user", "content": body_text})

    service_name = resource_attrs.get("service.name", "")

    return Invocation(
        timestamp=timestamp,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        estimated_cost_usd=cost,
        cost_priced=cost_from_source,
        system_prompt=system_prompt,
        tools=tools,
        messages=messages,
        response_text=response_text,
        max_tokens=attrs.get("gen_ai.request.max_tokens"),
        caller=caller,
        user_agent=service_name,
        operation=event_name or "otlp_log",
        request_id=attrs.get("request_id", attrs.get("gen_ai.request.id", "")),
        metadata=_build_metadata(attrs, resource_attrs, session_key),
    )


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

    messages = []
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})

    service_name = resource_attrs.get("service.name", "")

    return Invocation(
        timestamp=timestamp,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        estimated_cost_usd=cost,
        cost_priced=cost_from_source,
        system_prompt=system_prompt,
        tools=tools,
        messages=messages,
        response_text=response_text,
        max_tokens=attrs.get("gen_ai.request.max_tokens"),
        caller=caller,
        user_agent=service_name,
        operation=span_name or "otlp_span",
        request_id=attrs.get("request_id", attrs.get("gen_ai.request.id", "")),
        metadata=_build_metadata(attrs, resource_attrs, session_key),
    )


def _extract_model_and_tokens(attrs, resource_attrs):
    """Extract model ID and token counts from attributes, checking both
    Claude Code's native names and gen_ai.* convention names."""
    model = (attrs.get("model")
             or attrs.get("gen_ai.request.model")
             or attrs.get("gen_ai.response.model")
             or resource_attrs.get("gen_ai.request.model")
             or "unknown")
    input_tokens = as_int(attrs.get("input_tokens",
                                      attrs.get("gen_ai.usage.input_tokens", 0)))
    output_tokens = as_int(attrs.get("output_tokens",
                                       attrs.get("gen_ai.usage.output_tokens", 0)))
    cache_read_tokens = as_int(attrs.get("cache_read_tokens",
                                           attrs.get("gen_ai.usage.cache_read_input_tokens", 0)))
    cache_write_tokens = as_int(attrs.get("cache_creation_tokens",
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
