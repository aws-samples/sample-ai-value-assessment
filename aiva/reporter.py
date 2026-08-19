import json
from datetime import datetime, timezone


def generate_json_report(assessments, output_path, meta=None, show_samples=False):
    """Write a machine-readable JSON report (Prowler-style, one file).

    Structured for downstream tooling: a summary block plus one record per use
    case with verdict, the two orthogonal axes, aggregated metrics, and structured
    cost-optimisation checks. Raw prompt samples are only included when
    show_samples=True (off by default to protect employee privacy). `meta`
    carries run context (bucket, window, generated-at).
    """
    total_cost = sum(a["metrics"]["total_cost_usd"] for a in assessments)
    total_invocations = sum(a["metrics"]["invocation_count"] for a in assessments)
    total_sessions = sum(a["metrics"].get("session_count", 1) for a in assessments)

    def _rec(count_of):
        return len([a for a in assessments if a["recommendation"] == count_of])

    report = {
        "schema": "aiva.audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": meta or {},
        "summary": {
            "use_cases": len(assessments),
            "sessions_analysed": total_sessions,
            "invocations_analysed": total_invocations,
            "total_estimated_cost_usd": round(total_cost, 4),
            "stop": _rec("STOP"), "refine": _rec("REFINE"), "expand": _rec("EXPAND"),
        },
        "use_cases": [],
    }

    for a in assessments:
        m = a["metrics"]
        report["use_cases"].append({
            "name": a["name"],
            "description": a.get("description", ""),
            "recommendation": a["recommendation"],
            "verdict_reason": a.get("verdict_reason", ""),
            "category": a.get("category", "non_coding"),
            "nature": a.get("nature", "experimental"),
            "nature_reasoning": a.get("nature_reasoning", ""),
            "reasoning": a.get("reasoning", ""),
            "example_tasks": a.get("example_tasks", []),
            "refinement_suggestions": a.get("refinement_suggestions", []),
            "cost_optimizations": a.get("cost_optimizations", {}),
            "projected_monthly_cost_usd": round(a.get("projected_monthly_cost_usd", 0.0), 2),
            "estimated_monthly_projection": a.get("estimated_monthly_projection", ""),
            "metrics": {
                "session_count": m.get("session_count", 1),
                "invocation_count": m.get("invocation_count", 0),
                "total_input_tokens": m.get("total_input_tokens", 0),
                "total_output_tokens": m.get("total_output_tokens", 0),
                "total_cost_usd": round(m.get("total_cost_usd", 0.0), 4),
                "models_used": m.get("models_used", []),
                "caller_count": m.get("caller_count", 0),
            },
            "samples": [
                {
                    "user_message": s.get("user_message", "")[:800],
                    "response_preview": s.get("response_preview", "")[:800],
                }
                for sess in a.get("sessions", [])[:2]
                for s in sess.get("samples", [])[:2]
            ][:4] if show_samples else [],
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def generate_report(assessments, output_path, show_samples=False):
    """Generate a markdown report: Business view first, Technical view second."""
    stop = [a for a in assessments if a["recommendation"] == "STOP"]
    refine = [a for a in assessments if a["recommendation"] == "REFINE"]
    expand = [a for a in assessments if a["recommendation"] == "EXPAND"]

    total_cost = sum(a["metrics"]["total_cost_usd"] for a in assessments)
    total_invocations = sum(a["metrics"]["invocation_count"] for a in assessments)
    total_sessions = sum(a["metrics"].get("session_count", 1) for a in assessments)

    lines = []
    lines.append("# AI Value Assessment - Audit Report")
    lines.append(f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    lines.append("\n## Summary")
    lines.append("\n| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Distinct use cases | {len(assessments)} |")
    lines.append(f"| Sessions analysed | {total_sessions:,} |")
    lines.append(f"| Invocations analysed | {total_invocations:,} |")
    lines.append(f"| Total estimated cost | ${total_cost:.2f} |")
    lines.append(f"| Recommend STOP | {len(stop)} |")
    lines.append(f"| Recommend REFINE | {len(refine)} |")
    lines.append(f"| Recommend EXPAND | {len(expand)} |")
    if stop:
        stop_cost = sum(a["metrics"]["total_cost_usd"] for a in stop)
        lines.append(f"| Potential savings (STOP) | ${stop_cost:.2f} |")

    # -------------------------------------------------------------------
    # Business view
    # -------------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## Business View")
    lines.append("\nWhat is being done with AI, what it delivers, and what to do about it.\n")

    # Code generation is expected, low-insight usage - collapse it to one line
    # so the non-coding use cases (the point of the audit) lead.
    coding = [a for a in assessments if a.get("category") == "coding"]
    non_coding = [a for a in assessments if a.get("category") != "coding"]

    if coding:
        c_cost = sum(a["metrics"]["total_cost_usd"] for a in coding)
        c_sessions = sum(a["metrics"].get("session_count", 1) for a in coding)
        lines.append(f"\n_Software development assistance: {len(coding)} use case(s), "
                     f"{c_sessions} session(s), ${c_cost:.2f} - expected usage, collapsed. "
                     f"See Technical View for detail._\n")

    def section(title, items):
        items = [a for a in items if a.get("category") != "coding"]
        if not items:
            return
        cost = sum(a["metrics"]["total_cost_usd"] for a in items)
        lines.append(f"\n### {title} ({len(items)} use cases, ${cost:.2f})\n")
        for a in sorted(items, key=lambda x: x["metrics"]["total_cost_usd"], reverse=True):
            _write_business(lines, a)

    if not non_coding:
        lines.append("\n_No non-coding use cases identified in this window._\n")
    section("STOP - Kill These", stop)
    section("REFINE - Optimise These", refine)
    section("EXPAND - Invest More", expand)

    # -------------------------------------------------------------------
    # Technical view
    # -------------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## Technical View")
    lines.append("\nCost-optimisation detail and underlying sessions per use case.\n")

    for a in sorted(assessments, key=lambda x: x["metrics"]["total_cost_usd"], reverse=True):
        _write_technical(lines, a)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_business(lines, a):
    """Business-facing entry: use case, value, verdict, cost. No jargon."""
    m = a["metrics"]
    nature = a.get("nature", "experimental").capitalize()
    lines.append(f"#### {a['name']}  ·  {nature}")
    lines.append(f"\n{a['description']}")
    lines.append(f"\n**Cost:** ${m['total_cost_usd']:.2f} across "
                 f"{m.get('session_count', 1)} session(s) "
                 f"({m['invocation_count']:,} invocations)")
    if a.get("nature_reasoning"):
        lines.append(f"\n**Pattern:** {a['nature_reasoning']}")
    if a.get("example_tasks"):
        examples = " | ".join(f'"{e}"' for e in a["example_tasks"][:3])
        lines.append(f"\n**Examples:** {examples}")
    lines.append(f"\n**Why:** {a['reasoning']}")
    if a.get("estimated_monthly_projection"):
        lines.append(f"\n**If this continues:** {a['estimated_monthly_projection']}")
    lines.append("")


def _write_technical(lines, a):
    """Technical entry: cost optimisations + underlying sessions."""
    m = a["metrics"]
    nature = a.get("nature", "experimental").capitalize()
    lines.append(f"### {a['name']}  ·  {a['recommendation']}  ·  {nature}")
    lines.append(f"\n**Metrics:** {m.get('session_count', 1)} session(s) | "
                 f"{m['invocation_count']:,} invocations | "
                 f"{m['total_input_tokens']:,} input tokens | "
                 f"{m['total_output_tokens']:,} output tokens | "
                 f"${m['total_cost_usd']:.2f} | "
                 f"{m['caller_count']} caller(s) | "
                 f"Models: {', '.join(m['models_used'])}")

    opt = a.get("cost_optimizations", {})
    if opt:
        lines.append("\n**Cost optimisation:**")
        labels = {
            "model_right_sizing": "Model right-sizing",
            "prompt_caching": "Prompt caching",
            "tagging_compliance": "Cost tagging",
            "prompt_efficiency": "Prompt efficiency",
            "batching_opportunity": "Batching",
            "guardrails": "Guardrails",
        }
        marks = {"pass": "[pass]", "warn": "[warn]", "fail": "[fail]"}
        for key, label in labels.items():
            check = opt.get(key)
            if not check:
                continue
            if isinstance(check, dict):
                status, detail = check.get("status", "warn"), check.get("detail", "")
            else:
                status, detail = "warn", str(check)
            if not detail:
                continue
            mark = marks.get(status, "[warn]")
            lines.append(f"- {mark} **{label}:** {detail}")

    if a.get("refinement_suggestions"):
        lines.append("\n**Suggestions:**")
        for s in a["refinement_suggestions"]:
            lines.append(f"- {s}")

    sessions = a.get("sessions", [])
    if sessions:
        lines.append(f"\n**Underlying sessions ({len(sessions)}):**")
        for s in sessions:
            sm = s["metrics"]
            lines.append(f"- `{s['session_id']}` {s['activity']}: "
                         f": ${sm['total_cost_usd']:.2f}, "
                         f"{sm['invocation_count']} inv, {s.get('duration', '')}".rstrip(", "))

    lines.append("")
