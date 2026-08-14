import os
import sys
import tempfile

import click
from rich.console import Console

from datetime import datetime, timezone

from aiva.reader import read_invocation_logs_to_store
from aiva.classifier import classify_activities, cluster_use_cases, classify_use_cases, cap_use_cases
from aiva.reporter import generate_report, generate_json_report
from aiva.html_reporter import generate_html_report
from aiva.preflight import run_preflight
from aiva.store import AuditStore

console = Console()

_STATUS_STYLE = {"pass": "green", "warn": "yellow", "fail": "red"}
_STATUS_MARK = {"pass": "[ok]", "warn": "[warn]", "fail": "[FAIL]"}


@click.group()
def main():
    """AI Value Assessment - Audit your Bedrock AI spend."""
    pass


@main.command()
@click.option("--bucket", required=True, help="S3 bucket containing Model Invocation Logs")
@click.option("--prefix", default="bedrock-logs", help="S3 key prefix for logs")
@click.option("--region", default="us-west-2", help="AWS region")
@click.option("--days", default=7, type=int, help="Number of days of logs to analyse")
@click.option("--output", default="report", help="Output file path (without extension)")
@click.option("--format", "fmt", default="all", type=click.Choice(["html", "md", "json", "both", "all"]), help="Output format (all = html+md+json)")
@click.option("--model", default="us.anthropic.claude-sonnet-4-6", help="Bedrock model ID (inference profile) for classification")
@click.option("--skip-preflight", is_flag=True, default=False, help="Skip credential/bucket/model access checks (not recommended)")
@click.option("--db", default=None, help="Path to the local SQLite store (default: a temp file, deleted after the run)")
@click.option("--max-use-cases", default=50, type=int, help="Cap on named use cases (Pass 2b calls); overflow rolls into one 'Other' cluster. Set above your expected use-case count, cost per extra slot is a fraction of a cent")
def audit(bucket, prefix, region, days, output, fmt, model, skip_preflight, db, max_use_cases):
    """Run a full audit on Model Invocation Logs."""
    console.print(f"\n[bold]AI Value Assessment Audit[/bold]")
    console.print(f"  Bucket: s3://{bucket}/{prefix}")
    console.print(f"  Region: {region}")
    console.print(f"  Window: last {days} days")
    console.print()

    if not skip_preflight:
        console.print("[0/5] Checking credentials, bucket access, and model access...")
        if not _run_preflight_and_report(bucket, prefix, region, model):
            sys.exit(1)
        console.print()

    # Invocations are staged in a local SQLite file, not one in-memory list.
    # At org scale (hundreds of thousands to millions of invocations), the
    # full-payload content is tens of GB if held in memory for the whole run,
    # see docs/ARCHITECTURE.md's memory-ceiling section. --db lets a customer
    # point at a persistent path; the default is a temp file removed at exit.
    db_path = db
    cleanup_db = False
    if db_path is None:
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="aiva-")
        os.close(fd)
        cleanup_db = True

    try:
        _run_audit(bucket, prefix, region, days, output, fmt, model, db_path, max_use_cases)
    finally:
        if cleanup_db:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass


def _run_audit(bucket, prefix, region, days, output, fmt, model, db_path, max_use_cases):
    store = AuditStore(db_path)
    try:
        _run_audit_with_store(store, bucket, prefix, region, days, output, fmt, model, max_use_cases)
    finally:
        store.close()


def _run_audit_with_store(store, bucket, prefix, region, days, output, fmt, model, max_use_cases):
    # Step 1: Read logs from S3, streamed into the store in batches.
    console.print("[1/5] Reading invocation logs from S3...")
    read_invocation_logs_to_store(store, bucket, prefix, region, days)
    light_rows = store.fetch_light_rows()
    console.print(f"  Found {len(light_rows)} invocations")

    audit_count, audit_cost = store.overhead_summary()
    if audit_count:
        console.print(f"  Separated {audit_count} audit overhead calls (${audit_cost:.3f})")

    if not light_rows:
        console.print("\n[yellow]No invocations found. Logging may need more time to accumulate data.[/yellow]")
        return

    # Step 2: Group into sessions. Sessions carry invocation_ids, not full
    # payloads, light_rows never touched system_prompt/messages/response_text.
    console.print("[2/5] Grouping into sessions...")
    sessions = _group_into_sessions(light_rows)
    console.print(f"  Found {len(sessions)} sessions ({sum(s['invocation_count'] for s in sessions)} invocations)")

    # Step 3: Pass 1 - describe the business task behind each session.
    # Each worker fetches only its own session's full payload from the
    # store, classifies, then releases it, see classifier.classify_activities.
    console.print("[3/5] Identifying the business task behind each session...")
    activities = classify_activities(store, sessions, region, model)
    console.print(f"  Described {len(activities)} activities")

    # Step 4: Pass 2a + 2b - cluster into use cases, then classify each
    console.print("[4/5] Clustering activities into distinct use cases...")
    clusters = cluster_use_cases(activities, region, model)
    console.print(f"  Merged {len(activities)} activities into {len(clusters)} use cases")

    if len(clusters) > max_use_cases:
        overflow = len(clusters) - max_use_cases
        clusters = cap_use_cases(clusters, max_use_cases)
        console.print(f"  [yellow]Capped at {max_use_cases}: {overflow} lower-cost use case(s) "
                      f"rolled into 'Other' (raise --max-use-cases to see them individually)[/yellow]")

    console.print("      Classifying use cases (STOP / REFINE / EXPAND)...")
    assessments = classify_use_cases(clusters, region, model, window_days=days)

    # Step 5: Generate report
    console.print("[5/5] Generating report...")

    run_meta = {
        "bucket": bucket, "prefix": prefix, "region": region,
        "window_days": days, "classifier_model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if fmt in ("html", "both", "all"):
        html_path = f"{output}.html"
        generate_html_report(assessments, html_path)
        console.print(f"  [green]HTML report: {html_path}[/green]")

    if fmt in ("md", "both", "all"):
        md_path = f"{output}.md"
        generate_report(assessments, md_path)
        console.print(f"  [green]Markdown report: {md_path}[/green]")

    if fmt in ("json", "all"):
        json_path = f"{output}.json"
        generate_json_report(assessments, json_path, meta=run_meta)
        console.print(f"  [green]JSON report: {json_path}[/green]")

    # Show audit overhead
    if audit_count:
        console.print(f"\n  [dim]Audit overhead: {audit_count} calls, ${audit_cost:.3f}[/dim]")

    console.print()


def _run_preflight_and_report(bucket, prefix, region, model):
    """Run pre-flight checks, print results, return True iff safe to proceed.

    Fails fast on credential/bucket/model problems instead of letting them
    surface later as a raw traceback (no creds, wrong region, no bucket
    access) or, worse, as a report where every use case silently comes back
    "Task not identifiable" with no indication that Bedrock access was the
    real problem (model access denied).
    """
    checks = run_preflight(bucket, prefix, region, model)
    ok = True
    for c in checks:
        style = _STATUS_STYLE.get(c["status"], "white")
        mark = _STATUS_MARK.get(c["status"], "[?]")
        console.print(f"  [{style}]{mark}[/{style}] {c['name']}: {c['detail']}")
        if c["status"] == "fail":
            ok = False
            if c.get("hint"):
                console.print(f"        [dim]-> {c['hint']}[/dim]")
        elif c["status"] == "warn" and c.get("hint"):
            console.print(f"        [dim]-> {c['hint']}[/dim]")

    if not ok:
        console.print("\n[red]Pre-flight checks failed. Fix the issues above and re-run, "
                       "or pass --skip-preflight to bypass (not recommended).[/red]")
    return ok


# session_key grouping (explicit id from requestMetadata) is computed once in
# store.py at insert time and stored as a real SQL column, so it never needs
# to be re-derived from raw metadata on every grouping pass. See store.py's
# _session_key / _SESSION_ID_KEYS.


def _group_into_sessions(light_rows):
    """Group light invocation rows (from AuditStore.fetch_light_rows, never
    the heavy payload) into sessions.

    Sessions carry `invocation_ids`, not full invocation dicts. Pass 1 fetches
    the full rows for a session's ids from the store only when it actually
    classifies that session (see classifier._describe_activity), so grouping
    itself, and every session dict, stays proportional to metadata size, not
    to total prompt/response content. This is what makes the memory ceiling
    described in docs/ARCHITECTURE.md (holding the whole window's raw content
    in one Python list) not apply to this code path.

    Prefers session_key (an explicit session/conversation ID) when present;
    falls back to caller + 30-minute time-gap only for rows with none. Avoids
    collapsing distinct work into one giant "session" and avoids merging
    concurrent sessions from the same caller.
    """
    with_id = [r for r in light_rows if r.get("session_key")]
    without_id = [r for r in light_rows if not r.get("session_key")]

    sessions = []

    # 1. Explicit-ID grouping: one session per (caller, session-key).
    by_key = {}
    for r in with_id:
        key = (r.get("caller", "unknown"), r["session_key"])
        by_key.setdefault(key, []).append(r)

    for (caller, _sid), rows in by_key.items():
        sessions.append(_build_session(f"session_{len(sessions)}", caller, rows))

    # 2. Time-gap fallback for rows with no explicit ID.
    sessions.extend(_group_by_time_gap(without_id, start_index=len(sessions)))

    return sessions


def _build_session(session_id, caller, rows):
    """Assemble a session dict from its light rows, computing time bounds."""
    from datetime import datetime

    timestamps = []
    total_cost = 0.0
    for r in rows:
        total_cost += r.get("estimated_cost_usd", 0.0)
        try:
            timestamps.append(datetime.fromisoformat(r.get("timestamp", "").replace("Z", "+00:00")))
        except (ValueError, TypeError):
            pass

    return {
        "session_id": session_id,
        "caller": caller,
        "invocation_ids": [r["id"] for r in rows],
        "first_ts": min(timestamps) if timestamps else None,
        "last_ts": max(timestamps) if timestamps else None,
        "total_cost_usd": total_cost,
        "invocation_count": len(rows),
    }


def _group_by_time_gap(light_rows, start_index=0):
    """Fallback grouping: caller + 30-minute inactivity gap."""
    from datetime import datetime

    SESSION_GAP_MINUTES = 30

    sorted_rows = sorted(light_rows, key=lambda x: (x.get("caller", ""), x.get("timestamp", "")))

    sessions = []
    current_session = None

    for r in sorted_rows:
        caller = r.get("caller", "unknown")
        ts_str = r.get("timestamp", "")

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            ts = None

        # Start a new session on: first row, caller change, a >gap jump,
        # OR an unparseable timestamp (don't silently over-merge on bad clocks).
        new_session = (
            current_session is None
            or caller != current_session["caller"]
            or ts is None
            or current_session["last_ts"] is None
            or (ts - current_session["last_ts"]).total_seconds() > SESSION_GAP_MINUTES * 60
        )

        if new_session:
            current_session = {
                "session_id": f"session_{start_index + len(sessions)}",
                "caller": caller,
                "invocation_ids": [],
                "first_ts": ts,
                "last_ts": ts,
                "total_cost_usd": 0.0,
            }
            sessions.append(current_session)

        current_session["invocation_ids"].append(r["id"])
        if ts is not None:
            current_session["last_ts"] = ts
        current_session["total_cost_usd"] += r.get("estimated_cost_usd", 0.0)

    for s in sessions:
        s["invocation_count"] = len(s["invocation_ids"])

    return sessions
