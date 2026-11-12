"""Smoke tests for the pure-Python logic: pricing, session grouping, the
verdict rubric, the local store, and report generation. No AWS credentials
or network calls needed - the LLM never does arithmetic, so all of this is
independently testable.

Run: source .venv/bin/activate && pytest tests/ -v
"""
import json
import os

from aiva.pricing import get_pricing, PRICING_PER_1K, UNKNOWN_PRICING
from aiva.cli import _group_into_sessions
from aiva.classifier import _apply_verdict_rubric, project_monthly_cost, cap_use_cases, _build_cluster
from aiva.html_reporter import _use_case_id, generate_html_report
from aiva.otlp_reader import (
    _normalise_log_record, _normalise_span_record,
    _extract_resource_attrs, _nano_to_iso, _read_otlp_file,
)
from aiva.reporter import generate_report, generate_json_report
from aiva.store import AuditStore


# ---------------------------------------------------------------------------
# Pricing: exact-match, fail-loud, longest-match wins
# ---------------------------------------------------------------------------

def test_pricing_longest_match_wins():
    # opus-4-8 must not collide with a shorter "opus" prefix that isn't there.
    pricing, priced = get_pricing("global.anthropic.claude-opus-4-8-20260101-v1:0")
    assert priced is True
    assert pricing["input"] == PRICING_PER_1K["claude-opus-4-8"]["input"]
    assert pricing["output"] == PRICING_PER_1K["claude-opus-4-8"]["output"]


def test_pricing_unknown_model_fails_loud():
    pricing, priced = get_pricing("some.unknown.model-v9")
    assert priced is False
    assert pricing == UNKNOWN_PRICING


def test_pricing_sonnet_vs_opus_distinct_rates():
    sonnet, _ = get_pricing("us.anthropic.claude-sonnet-4-6-v1:0")
    opus, _ = get_pricing("us.anthropic.claude-opus-4-6-v1:0")
    assert sonnet != opus
    assert sonnet["input"] < opus["input"]


# ---------------------------------------------------------------------------
# Session grouping: explicit session_key, then time-gap fallback
# ---------------------------------------------------------------------------

def test_session_grouping_prefers_explicit_key():
    rows = [
        {"id": 1, "caller": "user-a", "session_key": "sess-1",
         "timestamp": "2026-01-01T00:00:00Z", "estimated_cost_usd": 0.10},
        {"id": 2, "caller": "user-a", "session_key": "sess-1",
         "timestamp": "2026-01-01T00:05:00Z", "estimated_cost_usd": 0.20},
        {"id": 3, "caller": "user-a", "session_key": "sess-2",
         "timestamp": "2026-01-01T00:06:00Z", "estimated_cost_usd": 0.05},
    ]
    sessions = _group_into_sessions(rows)
    assert len(sessions) == 2
    by_ids = {tuple(s["invocation_ids"]) for s in sessions}
    assert (1, 2) in by_ids
    assert (3,) in by_ids


def test_session_grouping_time_gap_fallback_splits_on_gap():
    rows = [
        {"id": 1, "caller": "user-b", "timestamp": "2026-01-01T00:00:00Z", "estimated_cost_usd": 0.01},
        {"id": 2, "caller": "user-b", "timestamp": "2026-01-01T00:10:00Z", "estimated_cost_usd": 0.01},
        # >30 min gap -> new session
        {"id": 3, "caller": "user-b", "timestamp": "2026-01-01T01:00:00Z", "estimated_cost_usd": 0.01},
    ]
    sessions = _group_into_sessions(rows)
    assert len(sessions) == 2
    assert sessions[0]["invocation_ids"] == [1, 2]
    assert sessions[1]["invocation_ids"] == [3]


def test_session_grouping_unparseable_timestamp_never_over_merges():
    rows = [
        {"id": 1, "caller": "user-c", "timestamp": "not-a-timestamp", "estimated_cost_usd": 0.01},
        {"id": 2, "caller": "user-c", "timestamp": "not-a-timestamp", "estimated_cost_usd": 0.01},
    ]
    sessions = _group_into_sessions(rows)
    # An unparseable timestamp must never be treated as "same instant, merge
    # in" - each row here starts its own session.
    assert len(sessions) == 2


# ---------------------------------------------------------------------------
# Verdict rubric: deterministic gates around the LLM's proposal
# ---------------------------------------------------------------------------

def _cluster(session_count=1, cost=0.5, invocation_count=5):
    return {
        "metrics": {
            "session_count": session_count,
            "total_cost_usd": cost,
            "invocation_count": invocation_count,
        },
        "sessions": [{"activity": "Summarise a document", "confidence": "high"}] * session_count,
    }


def test_rubric_stop_when_all_sessions_unidentifiable():
    cluster = {
        "metrics": {"session_count": 2, "total_cost_usd": 5.0, "invocation_count": 10},
        "sessions": [{"activity": "Task not identifiable"}, {"activity": "Task not identifiable"}],
    }
    rec, reason = _apply_verdict_rubric("REFINE", cluster, {}, {"nature": "experimental"})
    assert rec == "STOP"
    assert "abandoned" in reason.lower() or "not" in reason.lower()


def test_rubric_stop_on_negligible_one_off():
    cluster = _cluster(session_count=1, cost=0.20)
    rec, reason = _apply_verdict_rubric("REFINE", cluster, {}, {"nature": "experimental"})
    assert rec == "STOP"
    assert "negligible" in reason.lower()


def test_rubric_never_stops_merely_inefficient_work():
    # Real value, high cost, multiple sessions - inefficient but not evidence
    # of abandonment/negligible/deterministic. Must not be STOP.
    cluster = _cluster(session_count=5, cost=50.0, invocation_count=100)
    rec, _ = _apply_verdict_rubric("REFINE", cluster, {}, {"nature": "repeatable"})
    assert rec != "STOP"


def test_rubric_expand_requires_every_bar_clear():
    cluster = _cluster(session_count=5, cost=10.0, invocation_count=20)
    cost_opts = {"tagging_compliance": {"status": "pass"}}
    rec, reason = _apply_verdict_rubric(
        "EXPAND", cluster, cost_opts, {"nature": "repeatable"}
    )
    assert rec == "EXPAND"


def test_rubric_expand_downgraded_to_refine_on_cost_check_fail():
    cluster = _cluster(session_count=5, cost=10.0, invocation_count=20)
    cost_opts = {"tagging_compliance": {"status": "fail"}}
    rec, reason = _apply_verdict_rubric(
        "EXPAND", cluster, cost_opts, {"nature": "repeatable"}
    )
    assert rec == "REFINE"
    assert "tagging_compliance" in reason


# ---------------------------------------------------------------------------
# cap_use_cases: deterministic Python-side bound, protects Pass 2b cost
# even when LLM merge convergence at enterprise scale doesn't shrink far
# enough (see MERGE_MAX_DEPTH's comment).
# ---------------------------------------------------------------------------

def _activity(cost, caller="user-a"):
    return {
        "caller": caller,
        "metrics": {
            "session_count": 1, "invocation_count": 1,
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cost_usd": cost, "models_used": ["claude-sonnet-4-6"],
            "caller_count": 1, "tagged_count": 0, "guardrail_count": 0,
        },
    }


def _fake_cluster(name, cost):
    return _build_cluster(name, f"{name} description", [_activity(cost)])


def test_cap_use_cases_no_op_under_the_limit():
    clusters = [_fake_cluster(f"UC{i}", cost=1.0) for i in range(10)]
    result = cap_use_cases(clusters, max_use_cases=50)
    assert result == clusters


def test_cap_use_cases_keeps_highest_cost_and_rolls_up_the_rest():
    clusters = [_fake_cluster(f"UC{i}", cost=float(i)) for i in range(10)]
    result = cap_use_cases(clusters, max_use_cases=3)

    assert len(result) == 4  # 3 kept + 1 "Other"
    kept_names = {c["name"] for c in result[:3]}
    assert kept_names == {"UC9", "UC8", "UC7"}  # the 3 highest-cost, kept individually

    other = result[-1]
    assert other["name"] == "Other (long tail)"


def test_cap_use_cases_other_bucket_preserves_total_cost():
    clusters = [_fake_cluster(f"UC{i}", cost=float(i + 1)) for i in range(10)]
    total_before = sum(c["metrics"]["total_cost_usd"] for c in clusters)

    result = cap_use_cases(clusters, max_use_cases=3)
    total_after = sum(c["metrics"]["total_cost_usd"] for c in result)

    # Rolling overflow into "Other" must not lose or double-count spend.
    assert total_after == total_before


def test_rubric_defaults_to_refine():
    cluster = _cluster(session_count=3, cost=8.0, invocation_count=6)
    rec, reason = _apply_verdict_rubric("REFINE", cluster, {}, {"nature": "experimental"})
    assert rec == "REFINE"
    assert reason


# ---------------------------------------------------------------------------
# Cost projection: pure arithmetic, never the LLM
# ---------------------------------------------------------------------------

def test_monthly_projection_linear():
    assert project_monthly_cost(7.0, 7) == 30.0
    assert project_monthly_cost(0.0, 7) == 0.0
    assert project_monthly_cost(10.0, 0) == 0.0


# ---------------------------------------------------------------------------
# HTML use-case id: stable across regenerations (localStorage hide persists)
# ---------------------------------------------------------------------------

def test_use_case_id_stable_and_deterministic():
    a1 = {"name": "Fraud Pattern Summariser"}
    a2 = {"name": "fraud pattern summariser  "}  # same name, different case/whitespace
    assert _use_case_id(a1) == _use_case_id(a2)
    assert _use_case_id(a1).startswith("uc_")


def test_use_case_id_differs_for_different_names():
    assert _use_case_id({"name": "A"}) != _use_case_id({"name": "B"})


# ---------------------------------------------------------------------------
# Report generation: renders without crashing on a minimal assessment
# ---------------------------------------------------------------------------

def _assessment(name="Test Use Case", recommendation="REFINE"):
    return {
        "name": name,
        "description": "A test use case.",
        "recommendation": recommendation,
        "verdict_reason": "Real value with room to improve efficiency.",
        "category": "non_coding",
        "nature": "experimental",
        "nature_reasoning": "",
        "reasoning": "Looks fine.",
        "refinement_suggestions": [],
        "cost_optimizations": {
            "tagging_compliance": {"status": "pass", "detail": "All invocations tagged."},
        },
        "projected_monthly_cost_usd": 4.28,
        "estimated_monthly_projection": "~$4.28/month",
        "metrics": {
            "session_count": 1,
            "invocation_count": 5,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_cost_usd": 1.0,
            "models_used": ["claude-sonnet-4-6"],
            "caller_count": 1,
        },
        "sessions": [{
            "session_id": "session_0",
            "activity": "Summarise a document",
            "caller": "user-a",
            "duration": "5 minutes",
            "metrics": {"invocation_count": 5, "total_cost_usd": 1.0},
            "samples": [{
                "timestamp": "2026-01-01T00:00:00Z",
                "input_tokens": 200, "output_tokens": 100,
                "user_message": "Summarise this <script>alert(1)</script>",
                "response_preview": "Here is a summary.",
            }],
        }],
    }


def test_html_report_generates_and_escapes_content(tmp_path):
    out = tmp_path / "report.html"
    # Default: no samples rendered (privacy-safe)
    generate_html_report([_assessment()], str(out))
    html = out.read_text()
    assert "Test Use Case" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" not in html  # samples omitted entirely by default

    # With --show-samples: content must be HTML-escaped, not injected raw.
    out2 = tmp_path / "report_samples.html"
    generate_html_report([_assessment()], str(out2), show_samples=True)
    html2 = out2.read_text()
    assert "<script>alert(1)</script>" not in html2
    assert "&lt;script&gt;" in html2


def test_markdown_report_generates(tmp_path):
    out = tmp_path / "report.md"
    generate_report([_assessment()], str(out))
    md = out.read_text()
    assert "Test Use Case" in md
    assert "REFINE - Optimise These" in md


def test_json_report_generates_and_is_valid_json(tmp_path):
    out = tmp_path / "report.json"
    generate_json_report([_assessment()], str(out), meta={"bucket": "test-bucket"})
    data = json.loads(out.read_text())
    assert data["schema"] == "aiva.audit.v1"
    assert data["summary"]["refine"] == 1
    assert data["use_cases"][0]["name"] == "Test Use Case"


# ---------------------------------------------------------------------------
# AuditStore: real SQLite queries, no ORM. fetch_light_rows and
# fetch_full_invocations build their SQL from a hardcoded column list and a
# json_each-bound id array respectively (not string-joined identifiers or a
# variable-width placeholder clause), specifically to keep static analysis
# (bandit B608) from flagging safe, parameterized queries as injectable. This
# exercises the actual query execution end to end, not just the SQL text.
# ---------------------------------------------------------------------------

def _record(caller="user-a", cost=0.05, overhead=False):
    metadata = {"source": "aiva-audit"} if overhead else {"session_id": "sess-1"}
    return {
        "timestamp": "2026-01-01T00:00:00Z", "model": "claude-sonnet-4-6",
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "estimated_cost_usd": cost, "cost_priced": True,
        "caller": caller, "operation": "Converse", "request_id": "req-1",
        "max_tokens": 1000, "metadata": metadata,
        "system_prompt": "You are a helpful assistant.",
        "tools": [], "messages": [{"role": "user", "content": "hello"}],
        "response_text": "hi there", "user_agent": "test-agent",
    }


def test_store_fetch_light_rows_excludes_overhead_and_matches_column_order(tmp_path):
    store = AuditStore(str(tmp_path / "audit.db"))
    store.insert_invocations([
        (_record(caller="user-a"), "s3://bucket/key-1", "2026-01-01T00:00:00Z"),
        (_record(caller="user-b", overhead=True), "s3://bucket/key-2", "2026-01-01T00:01:00Z"),
    ])

    rows = store.fetch_light_rows()

    assert len(rows) == 1  # the overhead row is excluded
    assert rows[0]["caller"] == "user-a"
    assert set(rows[0].keys()) == {
        "id", "timestamp", "model", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "estimated_cost_usd",
        "cost_priced", "caller", "operation", "request_id", "max_tokens",
        "session_key", "is_audit_overhead",
    }
    store.close()


def test_store_fetch_full_invocations_returns_only_requested_ids_in_order(tmp_path):
    store = AuditStore(str(tmp_path / "audit.db"))
    store.insert_invocations([
        (_record(cost=0.01), "s3://bucket/key-1", "2026-01-01T00:00:00Z"),
        (_record(cost=0.02), "s3://bucket/key-2", "2026-01-01T00:01:00Z"),
        (_record(cost=0.03), "s3://bucket/key-3", "2026-01-01T00:02:00Z"),
    ])
    all_ids = [r["id"] for r in store.fetch_light_rows()]

    # Request a non-contiguous, reordered subset - fetch_full_invocations
    # must return exactly those two, ordered by id, not by request order.
    subset = [all_ids[2], all_ids[0]]
    full = store.fetch_full_invocations(subset)

    assert [r["id"] for r in full] == sorted(subset)
    assert {r["id"] for r in full} == set(subset)
    assert full[0]["system_prompt"] == "You are a helpful assistant."
    store.close()


def test_store_fetch_full_invocations_empty_ids_returns_empty(tmp_path):
    store = AuditStore(str(tmp_path / "audit.db"))
    assert store.fetch_full_invocations([]) == []
    store.close()


# ---------------------------------------------------------------------------
# OTLP reader: normalisation of OTLP-JSON log records and spans
# ---------------------------------------------------------------------------

def _otlp_attr(key, value, vtype="stringValue"):
    """Build an OTLP attribute dict."""
    return {"key": key, "value": {vtype: value}}


def _otlp_log_record(attrs=None, time_nano=1700000000_000000000, body_text=""):
    """Build a minimal OTLP log record."""
    record = {
        "timeUnixNano": str(time_nano),
        "attributes": attrs or [],
        "body": {"stringValue": body_text},
    }
    return record


def _otlp_resource_attrs(service_name="claude-code", enduser_id="user@example.com",
                          session_id="sess-abc"):
    """Build resource attributes in OTLP format."""
    attrs = [
        _otlp_attr("service.name", service_name),
    ]
    if enduser_id:
        attrs.append(_otlp_attr("enduser.id", enduser_id))
    if session_id:
        attrs.append(_otlp_attr("session.id", session_id))
    return {"resource": {"attributes": attrs}}


def test_otlp_extract_resource_attrs():
    entry = _otlp_resource_attrs(service_name="my-agent", enduser_id="alice")
    attrs = _extract_resource_attrs(entry)
    assert attrs["service.name"] == "my-agent"
    assert attrs["enduser.id"] == "alice"


def test_otlp_normalise_log_record_with_token_counts():
    log_attrs = [
        _otlp_attr("event.name", "claude_code.api_request"),
        _otlp_attr("gen_ai.request.model", "claude-sonnet-4-6"),
        _otlp_attr("gen_ai.usage.input_tokens", 1000, vtype="intValue"),
        _otlp_attr("gen_ai.usage.output_tokens", 200, vtype="intValue"),
        _otlp_attr("session.id", "sess-123"),
    ]
    resource_attrs = {"enduser.id": "test-user", "service.name": "claude-code"}

    record = _normalise_log_record(
        _otlp_log_record(attrs=log_attrs), resource_attrs
    )

    assert record is not None
    assert record["model"] == "claude-sonnet-4-6"
    assert record["input_tokens"] == 1000
    assert record["output_tokens"] == 200
    assert record["caller"] == "test-user"
    assert record["cost_priced"] is False  # OTLP cost is always an estimate
    assert record["estimated_cost_usd"] > 0
    assert record["metadata"]["session_id"] == "sess-123"
    assert record["metadata"]["source_format"] == "otlp"


def test_otlp_normalise_log_record_unknown_caller_when_no_enduser():
    log_attrs = [
        _otlp_attr("event.name", "claude_code.api_request"),
        _otlp_attr("gen_ai.request.model", "claude-haiku-3-5"),
        _otlp_attr("gen_ai.usage.input_tokens", 50, vtype="intValue"),
    ]
    resource_attrs = {"service.name": "claude-code"}

    record = _normalise_log_record(
        _otlp_log_record(attrs=log_attrs), resource_attrs
    )

    assert record is not None
    assert record["caller"] == "unknown"


def test_otlp_normalise_log_record_filters_irrelevant_events():
    log_attrs = [
        _otlp_attr("event.name", "http.client.request"),
    ]
    resource_attrs = {"service.name": "some-service"}

    record = _normalise_log_record(
        _otlp_log_record(attrs=log_attrs), resource_attrs
    )

    assert record is None


def test_otlp_normalise_log_record_captures_prompt_content():
    log_attrs = [
        _otlp_attr("event.name", "claude_code.user_prompt"),
        _otlp_attr("gen_ai.request.model", "claude-sonnet-4-6"),
        _otlp_attr("gen_ai.prompt", "Explain how S3 bucket policies work"),
        _otlp_attr("gen_ai.completion", "S3 bucket policies are JSON documents..."),
    ]
    resource_attrs = {"enduser.id": "dev-1", "service.name": "claude-code"}

    record = _normalise_log_record(
        _otlp_log_record(attrs=log_attrs), resource_attrs
    )

    assert record is not None
    assert len(record["messages"]) == 1
    assert record["messages"][0]["content"] == "Explain how S3 bucket policies work"
    assert "S3 bucket policies" in record["response_text"]


def test_otlp_normalise_span_record():
    span = {
        "name": "claude_code.llm_request",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000005000000000",
        "attributes": [
            _otlp_attr("gen_ai.request.model", "claude-opus-4-6"),
            _otlp_attr("gen_ai.usage.input_tokens", 5000, vtype="intValue"),
            _otlp_attr("gen_ai.usage.output_tokens", 1000, vtype="intValue"),
            _otlp_attr("session.id", "span-sess"),
        ],
    }
    resource_attrs = {"enduser.id": "span-user", "service.name": "claude-code"}

    record = _normalise_span_record(span, resource_attrs)

    assert record is not None
    assert record["model"] == "claude-opus-4-6"
    assert record["input_tokens"] == 5000
    assert record["caller"] == "span-user"
    assert record["operation"] == "claude_code.llm_request"


def test_otlp_nano_to_iso():
    assert _nano_to_iso(1700000000_000000000) == "2023-11-14T22:13:20+00:00"
    assert _nano_to_iso(0) == ""
    assert _nano_to_iso(None) == ""


def test_otlp_normalised_records_insert_into_store(tmp_path):
    """OTLP records go through the same store path as Bedrock records."""
    log_attrs = [
        _otlp_attr("event.name", "claude_code.api_request"),
        _otlp_attr("gen_ai.request.model", "claude-sonnet-4-6"),
        _otlp_attr("gen_ai.usage.input_tokens", 100, vtype="intValue"),
        _otlp_attr("gen_ai.usage.output_tokens", 50, vtype="intValue"),
        _otlp_attr("session.id", "otlp-sess-1"),
    ]
    resource_attrs = {"enduser.id": "otlp-user", "service.name": "claude-code"}

    record = _normalise_log_record(
        _otlp_log_record(attrs=log_attrs), resource_attrs
    )

    store = AuditStore(str(tmp_path / "audit.db"))
    store.insert_invocations([(record, "s3://bucket/otlp/file1.json", "2026-01-01T00:00:00Z")])

    rows = store.fetch_light_rows()
    assert len(rows) == 1
    assert rows[0]["caller"] == "otlp-user"
    assert rows[0]["session_key"] == "otlp-sess-1"
    assert rows[0]["model"] == "claude-sonnet-4-6"

    full = store.fetch_full_invocations([rows[0]["id"]])
    assert full[0]["metadata"]["source_format"] == "otlp"
    store.close()


def test_s3_key_prefix_handles_empty_and_slashed_prefixes():
    from aiva.reader import s3_key_prefix
    assert s3_key_prefix("", "AWSLogs") == "AWSLogs/"
    assert s3_key_prefix("bedrock-logs", "AWSLogs") == "bedrock-logs/AWSLogs/"
    assert s3_key_prefix("bedrock-logs/", "AWSLogs") == "bedrock-logs/AWSLogs/"
    assert s3_key_prefix("a/b", "AWSLogs") == "a/b/AWSLogs/"
    assert s3_key_prefix("otel/") == "otel/"
    assert s3_key_prefix("") == ""


def test_built_package_includes_every_aiva_subpackage():
    # A hard-coded package list once dropped aiva.prompts, breaking every non-editable install.
    import fnmatch
    import pathlib
    import tomllib
    root = pathlib.Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]
    subpackages = {".".join(init.parent.relative_to(root).parts) for init in (root / "aiva").rglob("__init__.py")}
    if isinstance(config.get("packages"), list):
        built = set(config["packages"])
    else:
        patterns = config["packages"]["find"]["include"]
        built = {pkg for pkg in subpackages if any(fnmatch.fnmatch(pkg, pat) for pat in patterns)}
    assert subpackages <= built, subpackages - built


# ---------------------------------------------------------------------------
# Reader: list only day partitions after the cutoff
# ---------------------------------------------------------------------------

class _FakeDelimiterS3:
    """list_objects_v2 paginator that answers Delimiter listings from a key set."""

    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, _name):
        return self

    def paginate(self, Bucket, Prefix, Delimiter=None):
        subdirs = sorted({Prefix + k[len(Prefix):].split("/", 1)[0] + "/"
                          for k in self.keys if k.startswith(Prefix) and "/" in k[len(Prefix):]})
        yield {"CommonPrefixes": [{"Prefix": p} for p in subdirs]}


def test_day_prefixes_lists_only_days_since_cutoff():
    from datetime import datetime, timedelta, timezone
    from aiva.reader import _day_prefixes
    root = "logs/AWSLogs/"
    s3 = _FakeDelimiterS3([f"{root}111/BedrockModelInvocationLogs/us-west-2/2026/01/01/00/a.json.gz"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    prefixes = _day_prefixes(s3, "b", root, cutoff)
    # cutoff day minus one, through today.
    assert len(prefixes) == 4
    assert all(p.startswith(f"{root}111/BedrockModelInvocationLogs/us-west-2/") for p in prefixes)
    assert prefixes[-1].endswith(datetime.now(timezone.utc).strftime("%Y/%m/%d/"))


def test_day_prefixes_falls_back_to_whole_prefix():
    from datetime import datetime, timezone
    from aiva.reader import _day_prefixes
    s3 = _FakeDelimiterS3(["logs/AWSLogs/custom-layout/x.json"])
    assert _day_prefixes(s3, "b", "logs/AWSLogs/", datetime.now(timezone.utc)) == ["logs/AWSLogs/"]


def test_pricing_matches_published_bedrock_rates():
    # AWS Pricing API, us-west-2, 2026-10-27, $ per 1M tokens.
    from aiva.pricing import estimate_cost
    m = 1_000_000
    cases = [
        ("global.anthropic.claude-opus-4-6-v1", 5.0, 25.0, 0.50, 6.25),
        ("us.anthropic.claude-opus-4-6-v1", 5.5, 27.5, 0.55, 6.875),
        ("arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.anthropic.claude-sonnet-4-6", 3.3, 16.5, 0.33, 4.125),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.1, 5.5, 0.11, 1.375),
        ("us.anthropic.claude-sonnet-4-20250514-v1:0", 3.0, 15.0, 0.30, 3.75),
    ]
    for model, inp, out, read, write in cases:
        for tokens, expected in [((m, 0, 0, 0), inp), ((0, m, 0, 0), out), ((0, 0, m, 0), read), ((0, 0, 0, m), write)]:
            cost, priced = estimate_cost(tokens[0], tokens[1], model, tokens[2], tokens[3])
            assert priced and abs(cost - expected) < 1e-6, (model, tokens, cost, expected)


def test_pricing_opus_5_5_cache_read_rate():
    from aiva.pricing import estimate_cost
    cost, _ = estimate_cost(0, 0, "global.anthropic.claude-opus-5-5", cache_read_tokens=1_000_000)
    assert abs(cost - 0.20) < 1e-9


def test_read_log_file_returns_every_record_in_object():
    import gzip as _gzip
    from aiva.reader import _read_log_file

    def rec(rid):
        return json.dumps({"modelId": "us.anthropic.claude-opus-5-5", "requestId": rid, "operation": "InvokeModel",
                           "input": {"inputTokenCount": 10, "inputBodyJson": {"messages": []}},
                           "output": {"outputTokenCount": 5, "outputBodyJson": {}}}, indent=1)

    body = _gzip.compress(("\n".join(rec(f"r{i}") for i in range(3)) + "\n").encode())

    class _S3:
        def get_object(self, Bucket, Key):
            class _B:
                def read(self_inner):
                    return body
            return {"Body": _B()}

    records = _read_log_file(_S3(), "b", "k.json.gz")
    assert [r["request_id"] for r in records] == ["r0", "r1", "r2"]


def test_read_log_file_keeps_every_record_in_a_batched_object():
    # Model Invocation Logging writes several records per object; each must survive.
    import gzip
    import io
    import json
    from aiva.reader import _read_log_file

    def record(i):
        return {"timestamp": f"2026-11-01T00:00:0{i}Z", "requestId": str(i),
                "modelId": "global.anthropic.claude-sonnet-4-6",
                "input": {"inputTokenCount": 10, "inputBodyJson": {"messages": []}},
                "output": {"outputTokenCount": 5, "outputBodyJson": {}}}

    body = gzip.compress("\n".join(json.dumps(record(i)) for i in range(3)).encode())

    class _S3:
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(body)}

    rows = _read_log_file(_S3(), "b", "AWSLogs/1/BedrockModelInvocationLogs/us-west-2/2026/11/01/00/x.json.gz")
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Classifier against a stubbed Bedrock client: no AWS calls
# ---------------------------------------------------------------------------

class _FakeBedrock:
    """Answers converse() with canned responses in order (the last one repeats).
    A str is model text, a dict is JSON-encoded, an Exception is raised, and a
    ("max_tokens", text) tuple is a truncated response."""

    def __init__(self, *responses):
        self.responses = list(responses)

    def converse(self, **_kwargs):
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        stop = "end_turn"
        if isinstance(r, tuple):
            stop, r = r
        text = r if isinstance(r, str) else json.dumps(r)
        return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": stop}


def _throttle():
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")


def _session(i, cost=4.0):
    return {"session_id": f"session_{i}", "caller": f"arn:caller{i}", "first_ts": None,
            "last_ts": None, "total_cost_usd": cost}


def _invocations(n=12, metadata=None):
    return [{"model": "us.anthropic.claude-sonnet-4-6", "cost_priced": True, "max_tokens": 4096,
             "metadata": {"team": "x"} if metadata is None else metadata,
             "messages": [{"role": "user", "content": "Draft the Q3 customer churn analysis for the board"}],
             "response_text": "Here is a structured churn analysis with three cohorts and a summary.",
             "input_tokens": 900, "output_tokens": 400}] * n


def test_bedrock_clients_use_adaptive_retries(monkeypatch):
    from aiva import classifier
    seen = []
    monkeypatch.setattr(classifier.boto3, "client", lambda *a, **kw: seen.append(kw) or _FakeBedrock({}))
    classifier.classify_use_cases([], "us-west-2", "m")
    classifier.cluster_use_cases([], "us-west-2", "m")
    assert len(seen) == 2
    for kw in seen:
        assert kw["config"].retries == {"mode": "adaptive", "max_attempts": 10}


def test_failed_pass1_never_becomes_stop():
    # Throttling or a refusal used to read as "Task not identifiable", then STOP rule 1.
    from aiva.classifier import _describe_activity, _classify_use_case
    reco = {"recommendation": "REFINE", "nature": "repeatable", "reasoning": "recurring analysis"}
    for failure in (_throttle(), "I can't help classify this content.", ("max_tokens", '{"activity": "Chur')):
        acts = [_describe_activity(_FakeBedrock(failure), _session(i), _invocations(), "m") for i in range(3)]
        out = _classify_use_case(_FakeBedrock(reco), _build_cluster("Churn", "", acts), "m", 7)
        assert out["recommendation"] != "STOP", failure
        assert all(a.get("classification_error") for a in acts)

    # A real unidentifiable session next to a failed one: STOP rule 1 judges only the real one.
    unclear = _describe_activity(_FakeBedrock({"activity": "Task not identifiable from logs"}),
                                 _session(9), _invocations(), "m")
    failed = _describe_activity(_FakeBedrock(_throttle()), _session(8), _invocations(), "m")
    assert not unclear.get("classification_error")
    out = _classify_use_case(_FakeBedrock(reco), _build_cluster("Churn", "", [unclear, failed]), "m", 7)
    assert out["recommendation"] == "STOP" and "1 session" in out["verdict_reason"]


def test_failed_pass2b_is_not_assessed_and_reports_render(tmp_path):
    from aiva.classifier import _describe_activity, _classify_use_case
    act = _describe_activity(_FakeBedrock({"activity": "Board churn analysis", "confidence": "high"}),
                             _session(0, cost=0.2), _invocations(), "m")
    cluster = _build_cluster("Churn", "", [act])
    for failure in (_throttle(), "Sorry, I cannot do that.", ("max_tokens", '{"recommendation": "RE')):
        out = _classify_use_case(_FakeBedrock(failure), cluster, "m", 7)
        assert out["recommendation"] == "NOT_ASSESSED", failure
    # A valid but empty answer is still a real (default) verdict, not a failure.
    assert _classify_use_case(_FakeBedrock({}), cluster, "m", 7)["recommendation"] != "NOT_ASSESSED"

    failed_act = _describe_activity(_FakeBedrock(_throttle()), _session(1), _invocations(), "m")
    failed_uc = _classify_use_case(_FakeBedrock({"recommendation": "STOP"}),
                                   _build_cluster("x", "", [failed_act]), "m", 7)
    assert failed_uc["recommendation"] == "NOT_ASSESSED"

    assessments = [out, failed_uc, _assessment()]
    generate_html_report(assessments, str(tmp_path / "r.html"))
    assert "NOT ASSESSED" in (tmp_path / "r.html").read_text()
    generate_report(assessments, str(tmp_path / "r.md"))
    assert "Not assessed (classification failed) | 2" in (tmp_path / "r.md").read_text()
    generate_json_report(assessments, str(tmp_path / "r.json"))
    summary = json.loads((tmp_path / "r.json").read_text())["summary"]
    assert summary["not_assessed"] == 2 and summary["sessions_not_classified"] == 1


def _stub_activity(i, cost=10.0, activity="Pricing research", inv=5, unpriced=0):
    return {"session_id": f"session_{i}", "activity": activity, "description": "desc",
            "business_value": "", "confidence": "high", "caller": f"arn:caller{i}",
            "duration": "", "samples": [],
            "metrics": {"invocation_count": inv, "total_input_tokens": 100, "total_output_tokens": 10,
                        "total_cost_usd": cost, "models_used": ["m"], "tagged_count": inv,
                        "guardrail_count": inv, "unpriced_count": unpriced}}


def test_repeated_ids_never_double_count_cost(monkeypatch):
    from aiva import classifier
    acts = [_stub_activity(i) for i in range(3)]
    pass2a = {"use_cases": [
        {"name": "Pricing research", "session_ids": ["session_0", "session_1"]},
        {"name": "Cost modelling", "session_ids": ["session_1", "session_2"]},
        {"name": "Dup within list", "session_ids": ["session_2", "session_2"]},
    ]}
    monkeypatch.setattr(classifier.boto3, "client", lambda *a, **kw: _FakeBedrock(pass2a))
    clusters = classifier.cluster_use_cases(acts, "us-west-2", "m")
    assert sum(c["metrics"]["total_cost_usd"] for c in clusters) == 30.0
    assert sum(c["metrics"]["session_count"] for c in clusters) == 3

    chunk = [_build_cluster(f"uc{i}", "", [a]) for i, a in enumerate(acts)]
    merge = {"merged": [{"name": "A", "cluster_ids": ["cluster_0", "cluster_1", "cluster_1"]},
                        {"name": "B", "cluster_ids": ["cluster_1", "cluster_2"]}]}
    merged = classifier._merge_one_batch(_FakeBedrock(merge), chunk, "m")
    assert sum(c["metrics"]["total_cost_usd"] for c in merged) == 30.0

    # The invariant check refuses a clustering that changed total cost.
    import pytest
    with pytest.raises(RuntimeError):
        classifier._check_partition(acts, [_build_cluster("x", "", acts + acts[:1])], "test")


def test_stop_deterministic_gate_matches_whole_words_only():
    cluster = _build_cluster("Meeting notes", "", [_stub_activity(i, cost=40, inv=50) for i in range(6)])
    def verdict(reasoning):
        parsed = {"recommendation": "STOP", "nature": "repeatable", "reasoning": reasoning}
        return _apply_verdict_rubric("STOP", cluster, {}, parsed)
    for text in ("Meeting transcripts are summarised but usage is dropping, STOP.",
                 "The session description is vague, so we suggest STOP.",
                 "Team ruled this pilot a failure last quarter, STOP.",
                 "Subscription renewal emails, STOP."):
        rec, reason = verdict(text)
        assert "deterministic" not in reason.lower(), text
    for text in ("A simple script would do this.", "Rule-based routing fits.",
                 "A regex covers it.", "Pure lookup work."):
        rec, reason = verdict(text)
        assert rec == "STOP" and "deterministic" in reason.lower(), text


def test_pricing_claude_3_and_opus_4_bedrock_ids():
    # Bedrock on-demand list prices, us-east-1, $ per 1M tokens.
    from aiva.pricing import estimate_cost
    m = 1_000_000
    cases = [
        ("anthropic.claude-3-5-sonnet-20240620-v1:0", 3.0, 15.0),
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 3.0, 15.0),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", 3.0, 15.0),
        ("anthropic.claude-3-haiku-20240307-v1:0", 0.25, 1.25),
        ("us.anthropic.claude-3-5-haiku-20241022-v1:0", 0.80, 4.0),
        ("us.anthropic.claude-opus-4-20250514-v1:0", 15.0, 75.0),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", 15.0, 75.0),
    ]
    for model, inp, out in cases:
        assert abs(estimate_cost(m, 0, model)[0] - inp) < 1e-6, model
        assert abs(estimate_cost(0, m, model)[0] - out) < 1e-6, model
    # Unconfirmed for us-east-1 on demand: stays unpriced rather than guessed.
    assert estimate_cost(m, m, "anthropic.claude-3-opus-20240229-v1:0") == (0.0, False)
    # Newer Opus 4.x keeps its own rate, not Opus 4's.
    assert abs(estimate_cost(m, 0, "global.anthropic.claude-opus-4-5-20251101-v1:0")[0] - 5.0) < 1e-6


def test_unpriced_invocations_block_trivial_spend_stop(tmp_path):
    from aiva.classifier import _describe_activity, _classify_use_case
    llama = [dict(inv, model="meta.llama3-1-70b-instruct-v1:0", cost_priced=False) for inv in _invocations(300)]
    act = _describe_activity(_FakeBedrock({"activity": "Contract review", "confidence": "high"}),
                             _session(0, cost=0.0), llama, "m")
    one_off = {"recommendation": "REFINE", "nature": "experimental", "reasoning": "one-off"}
    out = _classify_use_case(_FakeBedrock(one_off), _build_cluster("Contract review", "", [act]), "m", 7)
    assert out["recommendation"] != "STOP"
    assert "Partial" in out["estimated_monthly_projection"]

    assert act["metrics"]["unpriced_count"] == 300
    # OTLP marks table-priced cost as cost_priced=False; a known model is still priced.
    otlp = [dict(inv, cost_priced=False) for inv in _invocations(5)]
    assert _describe_activity(_FakeBedrock({"activity": "x"}), _session(1), otlp, "m")["metrics"]["unpriced_count"] == 0

    generate_report([out], str(tmp_path / "r.md"))
    assert "| Total estimated cost | unknown |" in (tmp_path / "r.md").read_text()
    generate_html_report([out], str(tmp_path / "r.html"))
    assert '<span class="uc-cost">unknown</span>' in (tmp_path / "r.html").read_text()
    generate_json_report([out], str(tmp_path / "r.json"))
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["summary"]["unpriced_invocations"] == 300
    assert data["use_cases"][0]["metrics"]["unpriced_invocation_count"] == 300


def test_tagging_check_ignores_session_ids_and_reader_keys():
    from aiva.classifier import _describe_activity, _check_tagging
    from aiva.otlp_reader import _build_metadata

    def tagging(md):
        act = _describe_activity(_FakeBedrock({"activity": "Release notes"}), _session(0),
                                 _invocations(20, metadata=md), "m")
        return _check_tagging(_build_cluster("x", "", [act])["metrics"])["status"]

    assert tagging(_build_metadata({}, {"service.name": "claude-code", "service.version": "2.1"}, "s1")) == "fail"
    assert tagging({"session_id": "3f1c", "conversationId": "c9"}) == "fail"
    assert tagging({"source": "my-app"}) == "fail"
    assert tagging({"session_id": "3f1c", "team": "pricing"}) == "pass"
    assert tagging(_build_metadata({"deployment.environment": "prod"}, {"service.name": "claude-code"}, "")) == "pass"
