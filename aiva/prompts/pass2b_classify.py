class Pass2bClassifyPrompt:
    """Assess a use case: STOP / REFINE / EXPAND + cost optimisation."""

    def build_prompt(self, cluster_name, cluster_description, sample_activities,
                     metrics, projected_monthly, window_days):
        m = metrics
        return f"""You are an AI spend auditor. Assess this business use case and recommend whether to STOP, REFINE, or EXPAND it.

## Use case
**Name:** {cluster_name}
**Description:** {cluster_description}

## Underlying activities in this use case
{sample_activities}

## Aggregated metrics
- Sessions: {m['session_count']}
- Invocations: {m['invocation_count']}
- Input tokens: {m['total_input_tokens']:,}
- Output tokens: {m['total_output_tokens']:,}
- Observed cost: ${m['total_cost_usd']:.2f} over {window_days} day(s)
- Projected monthly cost (already calculated for you): ${projected_monthly:.2f}
- Models used: {', '.join(m['models_used'])}
- Distinct callers: {m['caller_count']}

## Your task
Respond in this exact JSON format:
{{
    "recommendation": "STOP" | "REFINE" | "EXPAND",
    "category": "coding" | "non_coding",
    "nature": "experimental" | "repeatable",
    "nature_reasoning": "One sentence: why experimental or repeatable, and if repeatable, whether it looks like unmanaged shadow IT worth surfacing",
    "reasoning": "2-3 sentences on the BUSINESS value and efficiency of this use case",
    "example_tasks": ["Short verbatim-style example of a task in this use case", "Another example", "A third"],
    "refinement_suggestions": ["suggestion 1", "suggestion 2"],
    "cost_optimizations": {{
        "model_right_sizing": {{"status": "pass|warn|fail", "detail": "Is the cheapest capable model being used? Suggest specific alternatives if overpowered."}},
        "prompt_caching": {{"status": "pass|warn|fail", "detail": "Could prompt caching reduce cost? Identify repeated static content."}},
        "prompt_efficiency": {{"status": "pass|warn|fail", "detail": "Are prompts lean or bloated? Identify removable content."}},
        "batching_opportunity": {{"status": "pass|warn|fail", "detail": "Could invocations be consolidated?"}}
    }},
    "projection_note": "One sentence interpreting the ALREADY-CALCULATED projected monthly cost above (e.g. what it implies if usage scales). Do NOT compute or restate a different number - reference the ${projected_monthly:.2f} figure."
}}

For each cost_optimizations check, set status to: "pass" if already done well, "warn" if there is an opportunity to improve, "fail" if it is a clear problem. Put the explanation in detail.

Recommendation criteria:
- STOP: No task could be identified from the logs, or the work could be done without AI, or it appears broken/abandoned. Phrase this carefully: say the task "could not be identified from the available logs", NOT that it "has zero value" - absence of evidence in a log is not proof of no value.
- REFINE: Real value but inefficient (wrong model tier, bloated prompts, no caching)
- EXPAND: Clear value, efficient usage, worth scaling

Nature criteria (separate from the recommendation):
- experimental: One-off exploration, ad-hoc conversation, a trial, or a task unlikely to recur
- repeatable: A recurring pattern or standing workflow (e.g. an auto-transcriber that runs for every meeting, a nightly pipeline, a bot). If it recurs and is running outside sanctioned/managed channels, note it may be SHADOW IT worth surfacing to the platform/security team.

Category criteria:
- coding: Software engineering assistance - writing/editing/reviewing code, debugging, agentic dev tooling. This is expected, low-insight usage; it will be collapsed in the report.
- non_coding: Anything else - meeting/notes capture, customer-facing bots, content generation, data classification/extraction, research, pipelines. These are the interesting use cases the audit exists to surface.

Example tasks criteria:
- Provide 2-3 short, representative examples of what people actually asked/did in this use case
- Write them as paraphrased task descriptions, NOT verbatim quotes from the logs
- De-identify: remove names, project names, customer names, internal identifiers
- They should help someone reading the report instantly understand the flavour of the work
- Good: "Compare RDS vs Aurora pricing for multi-AZ deployment"
- Bad: "John asked about the Acme Corp database migration cost" (contains PII)"""
