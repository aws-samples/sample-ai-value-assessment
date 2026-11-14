import os
import sys
import tempfile

import boto3
import click
from rich.console import Console

from datetime import datetime, timezone

from aiva.ingest import read_to_store
from aiva.config import DEFAULT_MODEL, FORMATS, RunConfig, SourceConfig, load_config
from aiva.classifier import classify_activities, cluster_use_cases, classify_use_cases, cap_use_cases
from aiva.reporter import generate_report, generate_json_report
from aiva.html_reporter import generate_html_report
from aiva.preflight import run_preflight
from aiva.sources import SOURCES
from aiva.store import AuditStore

console = Console()

_STATUS_STYLE = {"pass": "green", "warn": "yellow", "fail": "red"}
_STATUS_MARK = {"pass": "[ok]", "warn": "[warn]", "fail": "[FAIL]"}


@click.group()
def main():
    """AI Value Assessment - Audit your AI spend."""
    pass


@main.command()
@click.option("--bucket", required=True, help="S3 bucket containing Model Invocation Logs")
@click.option("--prefix", default="bedrock-logs", help="S3 key prefix for logs")
@click.option("--region", default="us-west-2", help="AWS region")
@click.option("--days", default=7, type=int, help="Number of days of logs to analyse")
@click.option("--output", default="report", help="Output file path (without extension)")
@click.option("--format", "fmt", default="all", type=click.Choice(FORMATS), help="Output format (all = html+md+json)")
@click.option("--model", default=DEFAULT_MODEL, show_default=True, help="Bedrock model ID or inference profile ID/ARN for classification")
@click.option("--source", default="bedrock", type=click.Choice(list(SOURCES)), help="Log source format: bedrock (Model Invocation Logs) or otlp (OpenTelemetry JSON)")
@click.option("--skip-preflight", is_flag=True, default=False, help="Skip credential/bucket/model access checks (not recommended)")
@click.option("--db", default=None, help="Path to the local SQLite store (default: a temp file, deleted after the run)")
@click.option("--max-use-cases", default=50, type=int, help="Cap on named use cases (Pass 2b calls); overflow rolls into one 'Other' cluster. Set above your expected use-case count, cost per extra slot is a fraction of a cent")
@click.option("--show-samples", is_flag=True, default=False, help="Include raw prompt/response samples in the report (off by default to protect employee privacy)")
def audit(bucket, prefix, region, days, output, fmt, model, source, skip_preflight, db, max_use_cases, show_samples):
    """Run a full audit on one log source."""
    config = RunConfig(
        region=region, days=days, model=model, output=output, format=fmt,
        max_use_cases=max_use_cases, show_samples=show_samples,
        sources=[SourceConfig(type=source, bucket=bucket, prefix=prefix, region=region)],
    )
    _execute(config, skip_preflight, db)


@main.command()
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False), help="TOML file listing the log sources (see aiva.example.toml)")
@click.option("--skip-preflight", is_flag=True, default=False, help="Skip credential/bucket/model access checks (not recommended)")
@click.option("--db", default=None, help="Path to the local SQLite store (default: a temp file, deleted after the run)")
def run(config_path, skip_preflight, db):
    """Run an audit over every source in a config file, into one report."""
    try:
        config = load_config(config_path)
    except ValueError as e:
        raise click.ClickException(str(e))
    _execute(config, skip_preflight, db)


def _execute(config, skip_preflight, db):
    console.print(f"\n[bold]AI Value Assessment Audit[/bold]")
    for src in config.sources:
        console.print(f"  Source: {SOURCES[src.type].label}, s3://{src.bucket}/{src.prefix} ({src.region})")
    console.print(f"  Region: {config.region}")
    console.print(f"  Window: last {config.days} days")
    console.print()

    if not skip_preflight:
        console.print("[0/5] Checking credentials, bucket access, and model access...")
        if not _run_preflight_and_report(config):
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
        store = AuditStore(db_path)
        try:
            _run_audit_with_store(store, config)
        finally:
            store.close()
    finally:
        if cleanup_db:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass


def _run_audit_with_store(store, config):
    region, model, days = config.region, config.model, config.days

    # Step 1: Read logs from S3, streamed into the store in batches.
    for src in config.sources:
        console.print(f"[1/5] Reading {SOURCES[src.type].reading} from s3://{src.bucket}/{src.prefix}...")
        result = read_to_store(store, SOURCES[src.type], src.bucket, src.prefix, src.region, days)
        if result.skipped:
            console.print(f"  [yellow]Skipped {len(result.skipped)} unreadable objects or records, "
                          f"first: {result.skipped[0][0]} ({result.skipped[0][1]})[/yellow]")
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

    if len(clusters) > config.max_use_cases:
        overflow = len(clusters) - config.max_use_cases
        clusters = cap_use_cases(clusters, config.max_use_cases)
        console.print(f"  [yellow]Capped at {config.max_use_cases}: {overflow} lower-cost use case(s) "
                      f"rolled into 'Other' (raise --max-use-cases to see them individually)[/yellow]")

    console.print("      Classifying use cases (STOP / REFINE / EXPAND)...")
    assessments = classify_use_cases(clusters, region, model, window_days=days)

    # Step 5: Generate report
    console.print("[5/5] Generating report...")
    _write_reports(assessments, config)

    # Show audit overhead
    if audit_count:
        console.print(f"\n  [dim]Audit overhead: {audit_count} calls, ${audit_cost:.3f}[/dim]")

    console.print()


def _write_reports(assessments, config):
    """Write the reports to config.output, a local path or s3://bucket/key (no extension)."""
    run_meta = {
        "sources": [vars(src) for src in config.sources], "region": config.region,
        "window_days": config.days, "classifier_model": config.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # The OTLP caveat (estimated cost) applies when any source is OTLP.
    source = "otlp" if any(src.type == "otlp" for src in config.sources) else "bedrock"
    fmt, show_samples = config.format, config.show_samples

    to_s3 = config.output.startswith("s3://")
    out_dir = tempfile.mkdtemp(prefix="aiva-report-") if to_s3 else None
    output = os.path.join(out_dir, "report") if to_s3 else config.output

    written = []
    if fmt in ("html", "both", "all"):
        generate_html_report(assessments, f"{output}.html", show_samples=show_samples, source=source)
        written.append(".html")
    if fmt in ("md", "both", "all"):
        generate_report(assessments, f"{output}.md", show_samples=show_samples, source=source)
        written.append(".md")
    if fmt in ("json", "all"):
        generate_json_report(assessments, f"{output}.json", meta=run_meta, show_samples=show_samples)
        written.append(".json")

    if to_s3:
        bucket, _, key = config.output[5:].partition("/")
        s3 = boto3.client("s3", region_name=config.region)
        for ext in written:
            s3.upload_file(f"{output}{ext}", bucket, f"{key}{ext}")
            os.remove(f"{output}{ext}")
        os.rmdir(out_dir)
    for ext in written:
        console.print(f"  [green]Report: {config.output}{ext}[/green]")


def _run_preflight_and_report(config):
    """Run pre-flight checks, print results, return True iff safe to proceed.

    Fails fast on credential/bucket/model problems instead of letting them
    surface later as a raw traceback (no creds, wrong region, no bucket
    access) or, worse, as a report where every use case silently comes back
    "Task not identifiable" with no indication that Bedrock access was the
    real problem (model access denied).
    """
    checks = run_preflight(config.sources, config.region, config.model)
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
