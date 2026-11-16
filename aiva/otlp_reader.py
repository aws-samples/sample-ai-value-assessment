"""Read OpenTelemetry (OTLP-JSON) exports from S3. S3, threads and the store
live in aiva.ingest.

This is the transport layer only. It decodes OTLP JSON into OtelEvents and
hands each one to the first dialect in aiva.dialects that recognises it.
Which events are model calls, which carry context and which attributes hold
the model and tokens is the dialect's job, so product-specific rules never
live here.

The OTEL Collector's awss3 exporter (marshaler: otlp_json) writes files
containing the standard OTLP JSON structure: a top-level object with
`resourceLogs` (for log events) or `resourceSpans` (for traces). Each
resource entry carries resource-level attributes (service.name, enduser.id)
and scope-level records with per-event attributes.

Log events (resourceLogs) are the main signal. Trace spans (resourceSpans)
are read only when a document has no log records.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiva.dialects import DIALECTS
from aiva.ingest import Source, s3_key_prefix


@dataclass(frozen=True)
class OtelEvent:
    """One decoded log record or span, before any dialect reads it."""
    name: str               # event.name for a log record, the span name for a span
    attrs: dict = field(default_factory=dict)
    resource_attrs: dict = field(default_factory=dict)
    timestamp: str = ""
    body: str = ""
    is_span: bool = False


def log_prefix(prefix):
    return s3_key_prefix(prefix)


def list_prefixes(s3, bucket, base, cutoff):
    return [base]


def wants_key(relative_key):
    return relative_key.endswith((".json", ".gz", ".jsonl", ".ndjson"))


def parse(raw, fetch_body):
    """Convert one OTLP-JSON export document into Invocations and Attachments."""
    if not isinstance(raw, dict):
        return []
    records = [r for r in map(to_record, _log_events(raw)) if r]
    # Only fall back to spans when no log records were found, to avoid
    # double-counting the same LLM call reported as both a log and a span.
    if not records:
        records = [r for r in map(to_record, _span_events(raw)) if r]
    return records


def to_record(event):
    """The first matching dialect's Invocation or Attachment, or None."""
    for dialect in DIALECTS:
        if dialect.matches(event):
            return dialect.to_record(event)
    return None


def _log_events(raw):
    for resource_log in raw.get("resourceLogs", []):
        resource_attrs = _attrs(resource_log.get("resource", {}))
        for scope_log in resource_log.get("scopeLogs", []):
            for record in scope_log.get("logRecords", []):
                attrs = _attrs(record)
                body = record.get("body", {})
                yield OtelEvent(
                    name=attrs.get("event.name", attrs.get("name", "")),
                    attrs=attrs,
                    resource_attrs=resource_attrs,
                    timestamp=_nano_to_iso(record.get("timeUnixNano", record.get("observedTimeUnixNano", 0))),
                    body=body.get("stringValue", "") if isinstance(body, dict) else body if isinstance(body, str) else "",
                )


def _span_events(raw):
    for resource_span in raw.get("resourceSpans", []):
        resource_attrs = _attrs(resource_span.get("resource", {}))
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                yield OtelEvent(
                    name=span.get("name", ""),
                    attrs=_attrs(span),
                    resource_attrs=resource_attrs,
                    timestamp=_nano_to_iso(span.get("startTimeUnixNano", 0)),
                    is_span=True,
                )


def _attrs(entry):
    """An OTLP `attributes` list as a flat dict."""
    attrs = {}
    for attr in entry.get("attributes", []):
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


def _nano_to_iso(nano):
    """Convert nanosecond Unix timestamp to ISO-8601 string."""
    if not nano:
        return ""
    try:
        ts = int(nano) / 1_000_000_000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ""


SOURCE = Source(
    name="otlp",
    label="OTLP Telemetry",
    reading="OTLP telemetry logs",
    present_check="OTLP logs present",
    empty_hint=("This is expected on a brand-new setup. Confirm your OpenTelemetry "
                "Collector is writing to this prefix; check --prefix if you expected "
                "data immediately."),
    log_prefix=log_prefix,
    list_prefixes=list_prefixes,
    wants_key=wants_key,
    parse=parse,
)
