"""Smoke tests for the pure-Python logic: pricing, session grouping, the
verdict rubric, the local store, and report generation. No AWS credentials
or network calls needed - the LLM never does arithmetic, so all of this is
independently testable.

Run: source .venv/bin/activate && pytest tests/ -v
"""
import json
import os

from aiva.reader import _get_pricing, PRICING_PER_1K, UNKNOWN_PRICING
from aiva.cli import _group_into_sessions
from aiva.classifier import _apply_verdict_rubric, project_monthly_cost, cap_use_cases, _build_cluster
from aiva.html_reporter import _use_case_id, generate_html_report
from aiva.reporter import generate_report, generate_json_report
from aiva.store import AuditStore


# ---------------------------------------------------------------------------
# Pricing: exact-match, fail-loud, longest-match wins
# ---------------------------------------------------------------------------

def test_pricing_longest_match_wins():
    # opus-4-8 must not collide with a shorter "opus" prefix that isn't there.
    pricing, priced = _get_pricing("us.anthropic.claude-opus-4-8-20260101-v1:0")
    assert priced is True
    assert pricing == PRICING_PER_1K["claude-opus-4-8"]


def test_pricing_unknown_model_fails_loud():
    pricing, priced = _get_pricing("some.unknown.model-v9")
    assert priced is False
    assert pricing == UNKNOWN_PRICING


def test_pricing_sonnet_vs_opus_distinct_rates():
    sonnet, _ = _get_pricing("us.anthropic.claude-sonnet-4-6-v1:0")
    opus, _ = _get_pricing("us.anthropic.claude-opus-4-6-v1:0")
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
