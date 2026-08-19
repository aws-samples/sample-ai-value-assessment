import base64
import hashlib
import html as html_lib
from datetime import datetime, timezone

# Static, no per-report interpolation (checked: contains no Python f-string
# placeholders), so its content and CSP hash never change between reports.
# Kept as a plain string, not an f-string, so brace characters are literal
# JS syntax, not doubled for escaping.
_REPORT_SCRIPT = """
        document.querySelectorAll('.use-case-header').forEach(h => {
            h.addEventListener('click', () => h.closest('.use-case').classList.toggle('open'));
        });
        document.querySelectorAll('.session-item-header').forEach(h => {
            h.addEventListener('click', (e) => {
                e.stopPropagation();
                h.closest('.session-item').classList.toggle('open');
            });
        });

        // Category filter + admin hide. Hidden state persists per-browser via
        // localStorage keyed on a stable use-case id (survives re-audits).
        const cards = Array.from(document.querySelectorAll('.use-case'));
        const emptyMsg = document.querySelector('.filter-empty');
        const hiddenToggle = document.querySelector('.hidden-toggle');
        const STORE_KEY = 'aiva:hidden';
        let category = 'non_coding';
        let showHidden = false;

        function loadHidden() {
            try { return new Set(JSON.parse(localStorage.getItem(STORE_KEY)) || []); }
            catch (e) { return new Set(); }
        }
        function saveHidden(set) {
            try { localStorage.setItem(STORE_KEY, JSON.stringify([...set])); } catch (e) {}
        }
        let hidden = loadHidden();

        function render() {
            let shown = 0;
            cards.forEach(c => {
                const isHidden = hidden.has(c.dataset.id);
                const matchCat = category === 'all' || c.dataset.category === category;
                // Visible if it matches the category AND (isn't hidden, or we're showing hidden).
                const visible = matchCat && (!isHidden || showHidden);
                c.classList.toggle('filtered-out', !visible);
                if (visible) shown++;
            });
            if (emptyMsg) emptyMsg.classList.toggle('show', shown === 0);
            // Update the "Show hidden (N)" toggle.
            if (hiddenToggle) {
                const n = hidden.size;
                hiddenToggle.style.display = n > 0 ? '' : 'none';
                hiddenToggle.querySelector('.count').textContent = n;
                hiddenToggle.classList.toggle('active', showHidden);
                hiddenToggle.firstChild.textContent = showHidden ? 'Hide dismissed' : 'Show hidden';
            }
        }

        document.querySelectorAll('.filter-chip[data-filter]').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.filter-chip[data-filter]').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                category = chip.dataset.filter;
                render();
            });
        });

        if (hiddenToggle) {
            hiddenToggle.addEventListener('click', () => { showHidden = !showHidden; render(); });
        }

        document.querySelectorAll('.hide-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();  // don't toggle the card open
                const id = btn.closest('.use-case').dataset.id;
                hidden.add(id);
                saveHidden(hidden);
                render();
            });
        });

        // Cost-bar segment widths come from a data-pct attribute, not an
        // inline style attribute, so the CSP style-src can stay hash-only.
        document.querySelectorAll('.segment[data-pct]').forEach(seg => {
            seg.style.width = seg.dataset.pct + '%';
        });

        render();  // initial state: non-coding, hidden respected
"""

# CSP script-src hash for _REPORT_SCRIPT's exact content, computed from the
# source (not hand-copied), so an edit to the script above can never drift
# from the hash the browser checks against.
_REPORT_SCRIPT_CSP_HASH = base64.b64encode(
    hashlib.sha256(_REPORT_SCRIPT.encode("utf-8")).digest()
).decode("ascii")

# Static, no per-report interpolation, same reasoning as _REPORT_SCRIPT above.
# Dynamic values (cost-bar segment widths) are NOT inline style="..." - they
# are set via element.style.width in _REPORT_SCRIPT, which CSP's style-src
# does not govern (it only restricts style attributes/<style> blocks present
# in the HTML source, not runtime CSSOM writes from already-trusted script).
_REPORT_STYLE = """
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f1117;
            color: #e1e4e8;
            line-height: 1.6;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 24px 0;
            border-bottom: 1px solid #21262d;
            margin-bottom: 32px;
        }
        .header h1 { font-size: 24px; font-weight: 600; color: #f0f6fc; }
        .header .subtitle { color: #8b949e; font-size: 14px; margin-top: 4px; }
        .header .timestamp { color: #8b949e; font-size: 13px; }

        /* Summary Cards */
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }
        .card {
            background: #161b22;
            border: 1px solid #21262d;
            border-radius: 8px;
            padding: 20px;
        }
        .card .label { color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .card .value { font-size: 28px; font-weight: 700; margin-top: 4px; color: #f0f6fc; }
        .card .value.stop { color: #f85149; }
        .card .value.refine { color: #d29922; }
        .card .value.expand { color: #3fb950; }

        /* Cost Bar */
        .cost-bar-section {
            background: #161b22;
            border: 1px solid #21262d;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 32px;
        }
        .cost-bar-section h3 { margin-bottom: 16px; font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
        .cost-bar {
            height: 32px;
            border-radius: 6px;
            overflow: hidden;
            display: flex;
            margin-bottom: 12px;
        }
        .cost-bar .segment {
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 600;
            color: #0f1117;
            min-width: 40px;
        }
        .cost-bar .segment.stop { background: #f85149; }
        .cost-bar .segment.refine { background: #d29922; }
        .cost-bar .segment.expand { background: #3fb950; }
        .cost-legend { display: flex; gap: 24px; margin-top: 12px; }
        .cost-legend-item { font-size: 13px; display: flex; align-items: center; gap: 8px; }
        .cost-legend-dot { width: 12px; height: 12px; border-radius: 3px; }
        .cost-legend-dot.stop { background: #f85149; }
        .cost-legend-dot.refine { background: #d29922; }
        .cost-legend-dot.expand { background: #3fb950; }

        /* Use case rows */
        .section-title { font-size: 18px; font-weight: 600; margin: 32px 0 16px; color: #f0f6fc; }
        .use-case {
            background: #161b22;
            border: 1px solid #21262d;
            border-radius: 8px;
            margin-bottom: 12px;
            overflow: hidden;
            transition: border-color 0.2s;
        }
        .use-case:hover { border-color: #388bfd; }
        .use-case-header {
            display: grid;
            grid-template-columns: auto 1fr auto auto auto auto;
            align-items: center;
            gap: 16px;
            padding: 16px 20px;
            cursor: pointer;
        }
        .hide-btn {
            background: none; border: 1px solid #21262d; color: #8b949e;
            padding: 4px 10px; border-radius: 6px; font-size: 12px; cursor: pointer;
            transition: all 0.15s;
        }
        .hide-btn:hover { border-color: #f85149; color: #f85149; }
        .use-case.hidden-card { display: none; }
        .severity-badge {
            padding: 3px 10px;
            border-radius: 10px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }
        .severity-badge.stop { background: rgba(248, 81, 73, 0.15); color: #f85149; border: 1px solid rgba(248, 81, 73, 0.3); }
        .severity-badge.refine { background: rgba(210, 153, 34, 0.15); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.3); }
        .severity-badge.expand { background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid rgba(63, 185, 80, 0.3); }
        .nature-badge {
            padding: 3px 10px; border-radius: 10px; font-size: 10px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.3px; margin-left: 6px;
        }
        .nature-badge.repeatable { background: rgba(88, 166, 255, 0.15); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.3); }
        .nature-badge.experimental { background: rgba(139, 148, 158, 0.15); color: #8b949e; border: 1px solid rgba(139, 148, 158, 0.3); }
        .nature-badge.coding { background: rgba(139, 148, 158, 0.1); color: #6e7681; border: 1px solid rgba(139, 148, 158, 0.2); }

        /* Category filter */
        .filter-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .filter-bar .filter-label { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 4px; }
        .filter-chip {
            background: #161b22; border: 1px solid #21262d; color: #8b949e;
            padding: 5px 14px; border-radius: 16px; font-size: 13px; cursor: pointer;
            transition: all 0.15s;
        }
        .filter-chip:hover { border-color: #388bfd; color: #e1e4e8; }
        .filter-chip.active { background: rgba(56, 139, 253, 0.15); border-color: #388bfd; color: #58a6ff; font-weight: 600; }
        .filter-chip .count { opacity: 0.7; margin-left: 4px; }
        .filter-chip.hidden-toggle { margin-left: auto; display: none; }
        .use-case.filtered-out { display: none; }
        .filter-empty { display: none; padding: 24px; text-align: center; color: #8b949e; font-size: 14px; }
        .filter-empty.show { display: block; }
        .shadow-it {
            margin-top: 10px; padding: 8px 12px;
            background: rgba(88, 166, 255, 0.08);
            border: 1px solid rgba(88, 166, 255, 0.2);
            border-radius: 4px; font-size: 12px; color: #58a6ff;
        }
        .uc-title { font-size: 15px; font-weight: 500; color: #f0f6fc; }
        .uc-title .uc-desc { display: block; font-size: 12px; color: #8b949e; font-weight: 400; margin-top: 2px; }
        .uc-meta { font-size: 12px; color: #8b949e; white-space: nowrap; text-align: right; }
        .uc-cost { font-size: 18px; font-weight: 700; color: #f0f6fc; }
        .uc-chevron { color: #8b949e; transition: transform 0.2s; font-size: 12px; }
        .use-case.open .uc-chevron { transform: rotate(90deg); }
        .use-case-body {
            display: none;
            padding: 0 20px 20px;
            border-top: 1px solid #21262d;
        }
        .use-case.open .use-case-body { display: block; padding-top: 16px; }
        .use-case-body p { margin-bottom: 10px; font-size: 13px; color: #c9d1d9; }

        /* Business summary block */
        .biz-summary {
            background: rgba(88, 166, 255, 0.06);
            border: 1px solid rgba(88, 166, 255, 0.15);
            border-radius: 6px;
            padding: 12px 14px;
            margin-bottom: 14px;
        }
        .biz-summary .why { font-size: 13px; color: #c9d1d9; }
        .biz-summary .value-line { font-size: 12px; color: #58a6ff; margin-top: 6px; }

        /* Tech view divider */
        .tech-divider {
            font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
            color: #58a6ff; margin: 16px 0 8px; padding-top: 12px;
            border-top: 1px dashed #21262d;
        }

        /* Metrics row */
        .metrics-row {
            display: flex; gap: 16px; flex-wrap: wrap; margin: 12px 0;
            padding: 10px; background: #0d1117; border-radius: 4px;
        }
        .metric { text-align: center; }
        .metric .val { font-size: 14px; font-weight: 600; color: #f0f6fc; }
        .metric .lbl { font-size: 10px; color: #8b949e; }

        /* Cost checks */
        .cost-checks { margin-top: 12px; }
        .cost-checks h4 { font-size: 11px; text-transform: uppercase; color: #58a6ff; margin-bottom: 8px; letter-spacing: 0.5px; }
        .check-item {
            display: grid; grid-template-columns: 16px 120px 1fr; gap: 6px;
            align-items: start; margin-bottom: 6px; font-size: 11px;
        }
        .check-icon { font-size: 12px; }
        .check-icon.pass { color: #3fb950; }
        .check-icon.fail { color: #f85149; }
        .check-icon.warn { color: #d29922; }
        .check-label { color: #8b949e; font-weight: 500; }
        .check-detail { color: #c9d1d9; }

        /* Projection */
        .projection {
            margin-top: 10px; padding: 8px 12px;
            background: rgba(210, 153, 34, 0.08);
            border: 1px solid rgba(210, 153, 34, 0.2);
            border-radius: 4px; font-size: 11px; color: #d29922;
        }

        /* Suggestions */
        .suggestions { margin-top: 10px; }
        .suggestions li {
            font-size: 12px; color: #c9d1d9; margin-bottom: 6px;
            padding-left: 14px; position: relative; list-style: none;
        }
        .suggestions li::before { content: "\\2192"; position: absolute; left: 0; color: #d29922; }

        /* Underlying sessions */
        .sessions-block { margin-top: 14px; }
        .sessions-block h4 { font-size: 11px; text-transform: uppercase; color: #58a6ff; margin-bottom: 8px; letter-spacing: 0.5px; }
        .session-item {
            background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
            margin-bottom: 8px; overflow: hidden;
        }
        .session-item-header {
            display: grid; grid-template-columns: 1fr auto auto auto; gap: 12px;
            align-items: center; padding: 10px 12px; cursor: pointer; font-size: 12px;
        }
        .session-item-header:hover { background: #161b22; }
        .si-activity { color: #e1e4e8; }
        .si-meta { font-size: 10px; color: #8b949e; white-space: nowrap; }
        .si-cost { font-size: 12px; font-weight: 600; color: #f0f6fc; }
        .si-chevron { color: #8b949e; font-size: 9px; transition: transform 0.2s; }
        .session-item.open .si-chevron { transform: rotate(90deg); }
        .session-item-body { display: none; padding: 0 12px 12px; border-top: 1px solid #161b22; }
        .session-item.open .session-item-body { display: block; padding-top: 10px; }

        .sample {
            background: #161b22; border: 1px solid #21262d; border-radius: 4px;
            padding: 10px; margin-bottom: 6px;
        }
        .sample .s-header { display: flex; justify-content: space-between; font-size: 10px; color: #8b949e; margin-bottom: 6px; }
        .sample .s-header .tokens { color: #d29922; }
        .sample .s-label { font-size: 9px; text-transform: uppercase; font-weight: 600; margin-bottom: 3px; }
        .sample .s-label.prompt { color: #3fb950; }
        .sample .s-label.response { color: #d29922; margin-top: 8px; }
        .sample .s-content {
            font-family: 'SF Mono', monospace; font-size: 10px; color: #c9d1d9;
            white-space: pre-wrap; word-break: break-word; max-height: 120px;
            overflow-y: auto; padding: 6px; background: #0d1117; border-radius: 3px;
        }
        .no-samples { font-size: 11px; color: #8b949e; }

        .footer {
            text-align: center; padding: 24px 0; border-top: 1px solid #21262d;
            color: #8b949e; font-size: 12px; margin-top: 32px;
        }

        @media (max-width: 768px) {
            .use-case-header { grid-template-columns: auto 1fr auto auto; }
            .uc-meta { display: none; }
        }
"""

_REPORT_STYLE_CSP_HASH = base64.b64encode(
    hashlib.sha256(_REPORT_STYLE.encode("utf-8")).digest()
).decode("ascii")


def _escape(text):
    """HTML-escape text for safe rendering."""
    if not text:
        return ""
    return html_lib.escape(str(text))


def _use_case_id(a):
    """Stable id for a use case, derived from its name.

    Persisted admin actions (hide) key off this, so it must be stable across
    report regenerations - positional session ids are not. Same use-case name
    -> same id -> hidden state survives the next audit.
    """
    name = (a.get("name") or "unnamed").strip().lower()
    return "uc_" + hashlib.sha256(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def generate_html_report(assessments, output_path, show_samples=False):
    """Generate a use-case HTML dashboard: Business summary + Technical drill-down.

    Each assessment is a de-duplicated business USE CASE that may span several
    sessions. The card shows the business view; expanding reveals the technical
    view (cost optimisations, underlying sessions, and optionally prompt samples).

    By default, raw prompt/response samples and caller identities are omitted to
    protect employee privacy (aggregate analysis only). Pass show_samples=True
    to include them.
    """
    stop = [a for a in assessments if a["recommendation"] == "STOP"]
    refine = [a for a in assessments if a["recommendation"] == "REFINE"]
    expand = [a for a in assessments if a["recommendation"] == "EXPAND"]

    total_cost = sum(a["metrics"]["total_cost_usd"] for a in assessments)
    total_invocations = sum(a["metrics"]["invocation_count"] for a in assessments)
    total_sessions = sum(a["metrics"].get("session_count", 1) for a in assessments)
    stop_cost = sum(a["metrics"]["total_cost_usd"] for a in stop)
    refine_cost = sum(a["metrics"]["total_cost_usd"] for a in refine)
    expand_cost = sum(a["metrics"]["total_cost_usd"] for a in expand)

    # Non-coding use cases lead (the point of the audit); coding sinks to the
    # bottom. Within each group, rank by cost.
    all_sorted = sorted(
        assessments,
        key=lambda x: (x.get("category") == "coding", -x["metrics"]["total_cost_usd"]),
    )
    use_cases_html = _render_use_case_list(all_sorted, show_samples=show_samples)

    coding_count = sum(1 for a in assessments if a.get("category") == "coding")
    noncoding_count = len(assessments) - coding_count

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'sha256-{_REPORT_STYLE_CSP_HASH}'; script-src 'sha256-{_REPORT_SCRIPT_CSP_HASH}'">
    <title>AI Value Assessment Audit Report</title>
    <style>{_REPORT_STYLE}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>AI Value Assessment</h1>
                <div class="subtitle">Model Invocation Audit Report</div>
            </div>
            <div class="timestamp">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>
        </div>

        <div class="summary-grid">
            <div class="card">
                <div class="label">Total Spend</div>
                <div class="value">${total_cost:.2f}</div>
            </div>
            <div class="card">
                <div class="label">Use Cases</div>
                <div class="value">{len(assessments)}</div>
            </div>
            <div class="card">
                <div class="label">Sessions</div>
                <div class="value">{total_sessions}</div>
            </div>
            <div class="card">
                <div class="label">Invocations</div>
                <div class="value">{total_invocations:,}</div>
            </div>
            <div class="card">
                <div class="label">Stop</div>
                <div class="value stop">{len(stop)}</div>
            </div>
            <div class="card">
                <div class="label">Refine</div>
                <div class="value refine">{len(refine)}</div>
            </div>
            <div class="card">
                <div class="label">Expand</div>
                <div class="value expand">{len(expand)}</div>
            </div>
        </div>

        <div class="cost-bar-section">
            <h3>Cost by Recommendation</h3>
            {_render_cost_bar(stop_cost, refine_cost, expand_cost, total_cost)}
            <div class="cost-legend">
                <div class="cost-legend-item"><div class="cost-legend-dot stop"></div>${stop_cost:.2f} wasted (Stop)</div>
                <div class="cost-legend-item"><div class="cost-legend-dot refine"></div>${refine_cost:.2f} inefficient (Refine)</div>
                <div class="cost-legend-item"><div class="cost-legend-dot expand"></div>${expand_cost:.2f} delivering value (Expand)</div>
            </div>
        </div>

        <div class="section-title">Use Cases ({len(assessments)})</div>
        <div class="filter-bar">
            <span class="filter-label">Show</span>
            <button class="filter-chip active" data-filter="non_coding">Non-coding<span class="count">{noncoding_count}</span></button>
            <button class="filter-chip" data-filter="coding">Code-gen<span class="count">{coding_count}</span></button>
            <button class="filter-chip" data-filter="all">All<span class="count">{len(assessments)}</span></button>
            <button class="filter-chip hidden-toggle">Show hidden<span class="count">0</span></button>
        </div>
        {use_cases_html}
        <div class="filter-empty">No use cases in this category.</div>

        <div class="footer">
            AI Value Assessment v1.0.0 &middot; Data stays in your account &middot; Business view + technical drill-down
            <br><span style="color:#d29922;font-size:11px;">Note: Example tasks are model-generated paraphrases, not verbatim quotes. Despite de-identification instructions, they may inadvertently contain PII. Review before sharing externally.</span>
        </div>
    </div>

    <script>{_REPORT_SCRIPT}</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _render_use_case_list(assessments, show_samples=False):
    return "".join(_render_use_case(a, show_samples=show_samples) for a in assessments)


def _render_use_case(a, show_samples=False):
    """One use case: business summary in the header/body, technical view on expand."""
    m = a["metrics"]
    rec = a["recommendation"].lower()
    name = a.get("name", "Unnamed use case")
    description = a.get("description", "")
    reasoning = a.get("reasoning", "")
    verdict_reason = a.get("verdict_reason", "")
    session_count = m.get("session_count", 1)
    nature = a.get("nature", "experimental").lower()
    nature_reasoning = a.get("nature_reasoning", "")

    # Surface likely shadow IT: a recurring workflow flagged in its reasoning
    pattern_html = ""
    if nature_reasoning:
        is_shadow = "shadow" in nature_reasoning.lower()
        cls = "shadow-it" if is_shadow else "biz-summary"
        prefix = "Shadow IT signal: " if is_shadow else "Pattern: "
        pattern_html = f'<div class="{cls}"><strong>{prefix}</strong>{_escape(nature_reasoning)}</div>'

    # Business value pulled from the underlying activities (Pass 1)
    value_line = ""
    for s in a.get("sessions", []):
        if s.get("business_value"):
            value_line = s["business_value"]
            break

    projection_html = ""
    if a.get("estimated_monthly_projection"):
        projection_html = (f'<div class="projection"><strong>If this continues:</strong> '
                           f'{_escape(a["estimated_monthly_projection"])}</div>')

    suggestions_html = ""
    if a.get("refinement_suggestions"):
        items = "".join(f'<li>{_escape(s)}</li>' for s in a["refinement_suggestions"])
        suggestions_html = f'<ul class="suggestions">{items}</ul>'

    examples_html = ""
    if a.get("example_tasks"):
        items = " &middot; ".join(f'"{_escape(e)}"' for e in a["example_tasks"][:3])
        examples_html = f'<div class="value-line" style="margin-top:6px;"><strong>Examples:</strong> {items}</div>'

    value_html = f'<div class="value-line">Business value: {_escape(value_line)}</div>' if value_line else ""

    is_coding = a.get("category") == "coding"
    category = "coding" if is_coding else "non_coding"
    category_badge = '<span class="nature-badge coding">code-gen</span>' if is_coding else ""

    uc_id = _use_case_id(a)

    return f"""
    <div class="use-case" data-category="{category}" data-id="{uc_id}">
        <div class="use-case-header">
            <span><span class="severity-badge {rec}">{a['recommendation']}</span><span class="nature-badge {nature}">{_escape(nature)}</span>{category_badge}</span>
            <span class="uc-title">{_escape(name)}<span class="uc-desc">{_escape(description)}</span></span>
            <span class="uc-meta">{session_count} session(s)<br>{m['invocation_count']:,} calls</span>
            <span class="uc-cost">${m['total_cost_usd']:.2f}</span>
            <button class="hide-btn" title="Hide this use case">Hide</button>
            <span class="uc-chevron">&#9654;</span>
        </div>
        <div class="use-case-body">
            <div class="biz-summary">
                <div class="verdict-reason"><strong>Why {a['recommendation']}:</strong> {_escape(verdict_reason)}</div>
                <div class="why">{_escape(reasoning)}</div>
                {value_html}
                {examples_html}
            </div>
            {pattern_html}
            {projection_html}
            {suggestions_html}

            <div class="tech-divider">Technical detail</div>
            <div class="metrics-row">
                <div class="metric"><div class="val">{m['invocation_count']:,}</div><div class="lbl">Invocations</div></div>
                <div class="metric"><div class="val">{m['total_input_tokens']:,}</div><div class="lbl">Input Tokens</div></div>
                <div class="metric"><div class="val">{m['total_output_tokens']:,}</div><div class="lbl">Output Tokens</div></div>
                <div class="metric"><div class="val">{m['caller_count']}</div><div class="lbl">Callers</div></div>
                <div class="metric"><div class="val">{_escape(', '.join(_short_models(m['models_used'])))}</div><div class="lbl">Model(s)</div></div>
            </div>
            {_render_cost_checks(a)}
            {_render_sessions(a, show_samples=show_samples)}
        </div>
    </div>"""


def _short_models(models):
    """Trim inference-profile ARNs down to the readable model name."""
    out = []
    for mdl in models[:3]:
        out.append(mdl.split("/")[-1] if "/" in mdl else mdl)
    return out


def _render_cost_checks(a):
    """Render cost optimization checklist."""
    checks = a.get("cost_optimizations", {})
    if not checks:
        return ""

    check_labels = {
        "model_right_sizing": "Model Sizing",
        "prompt_caching": "Prompt Caching",
        "tagging_compliance": "Cost Tagging",
        "prompt_efficiency": "Prompt Efficiency",
        "batching_opportunity": "Batching",
        "guardrails": "Guardrails",
    }

    icons = {"pass": "&#10003;", "warn": "&#9888;", "fail": "&#10007;"}

    items = []
    for key, label in check_labels.items():
        check = checks.get(key)
        if not check:
            continue

        # Structured {status, detail}; tolerate a legacy bare string.
        if isinstance(check, dict):
            status = check.get("status", "warn")
            detail = check.get("detail", "")
        else:
            status, detail = "warn", str(check)
        if not detail:
            continue

        status = status if status in icons else "warn"
        items.append(f'''<div class="check-item">
            <span class="check-icon {status}">{icons[status]}</span>
            <span class="check-label">{label}</span>
            <span class="check-detail">{_escape(detail)}</span>
        </div>''')

    if not items:
        return ""

    return f'''<div class="cost-checks">
        <h4>Cost Optimization Checks</h4>
        {"".join(items)}
    </div>'''


def _render_sessions(a, show_samples=False):
    """Render the underlying sessions (and optionally prompt samples)."""
    sessions = a.get("sessions", [])
    if not sessions:
        return ""

    rows = []
    for s in sessions:
        sm = s["metrics"]
        duration = s.get("duration", "")

        samples_html = ""
        if show_samples:
            for smp in s.get("samples", []):
                samples_html += f'''<div class="sample">
                    <div class="s-header">
                        <span>{_escape(smp.get("timestamp", ""))}</span>
                        <span class="tokens">{smp.get("input_tokens", 0):,} in / {smp.get("output_tokens", 0):,} out</span>
                    </div>
                    <div class="s-label prompt">User Prompt</div>
                    <div class="s-content">{_escape(smp.get("user_message", "(empty)"))}</div>
                    <div class="s-label response">Response</div>
                    <div class="s-content">{_escape(smp.get("response_preview", "(empty)"))}</div>
                </div>'''
            if not samples_html:
                samples_html = '<p class="no-samples">No prompt samples captured.</p>'

        rows.append(f'''<div class="session-item">
            <div class="session-item-header">
                <span class="si-activity">{_escape(s.get("activity", "Activity"))}{(" &middot; " + _escape(duration)) if duration else ""}</span>
                <span class="si-meta">{sm['invocation_count']} inv</span>
                <span class="si-cost">${sm['total_cost_usd']:.2f}</span>
                {"<span class='si-chevron'>&#9654;</span>" if show_samples else ""}
            </div>
            {"<div class='session-item-body'>" + samples_html + "</div>" if show_samples else ""}
        </div>''')

    return f'''<div class="sessions-block">
        <h4>Underlying sessions ({len(sessions)})</h4>
        {"".join(rows)}
    </div>'''


def _render_cost_bar(stop_cost, refine_cost, expand_cost, total_cost):
    # Width is set via data-pct + a script.style.width write (see
    # _REPORT_SCRIPT), not an inline style attribute, so the CSP style-src
    # can stay hash-only.
    if total_cost == 0:
        return '<div class="cost-bar"><div class="segment refine" data-pct="100">No data</div></div>'

    stop_pct = (stop_cost / total_cost) * 100
    refine_pct = (refine_cost / total_cost) * 100
    expand_pct = (expand_cost / total_cost) * 100

    segments = []
    if stop_pct > 0:
        segments.append(f'<div class="segment stop" data-pct="{stop_pct:.1f}">${stop_cost:.2f}</div>')
    if refine_pct > 0:
        segments.append(f'<div class="segment refine" data-pct="{refine_pct:.1f}">${refine_cost:.2f}</div>')
    if expand_pct > 0:
        segments.append(f'<div class="segment expand" data-pct="{expand_pct:.1f}">${expand_cost:.2f}</div>')

    return f'<div class="cost-bar">{"".join(segments)}</div>'
