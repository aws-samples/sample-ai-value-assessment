import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

from aiva.pricing import estimate_cost, get_pricing
from aiva.prompts.common import make_nonce, untrusted_entry
from aiva.prompts.pass1_describe import Pass1DescribePrompt
from aiva.prompts.pass2a_cluster import Pass2aClusterPrompt
from aiva.prompts.pass2a_merge import Pass2aMergePrompt
from aiva.prompts.pass2b_classify import Pass2bClassifyPrompt
from aiva.store import _SESSION_ID_KEYS, AUDIT_SOURCE

_pass1 = Pass1DescribePrompt()
_pass2a = Pass2aClusterPrompt()
_pass2a_merge = Pass2aMergePrompt()
_pass2b = Pass2bClassifyPrompt()

# Allowed values for enum-like fields in model JSON (see _as_choice).
RECOMMENDATIONS = ("stop", "refine", "expand")
CATEGORIES = ("coding", "non_coding")
NATURES = ("experimental", "repeatable")
CONFIDENCES = ("high", "medium", "low")
CHECK_STATUSES = ("pass", "warn", "fail")

# EXPAND needs log-provable recurrence (see _apply_verdict_rubric).
EXPAND_MIN_SESSIONS = 3


# ---------------------------------------------------------------------------
# Three-pass semantic classification
#
#   Pass 1  (per session)      -> what business task was the human doing?
#   Pass 2a (one call, all)    -> cluster activities into distinct use cases
#   Pass 2b (per use case)     -> STOP / REFINE / EXPAND + cost optimisations
#
# We deliberately look PAST the tool (Claude Code, Codex, Amazon Q Developer,
# LibreChat, or any other agent harness, plus their system-reminder /
# tool-schema injections) and classify the underlying business value. The
# harness is a tool, not a use case.
# ---------------------------------------------------------------------------


PASS1_MAX_WORKERS = 8  # Bedrock has account-level TPS/RPM quotas; keep well under them.

# Adaptive retries back off on ThrottlingException instead of failing the call
# after the default 3 attempts. A call that still fails is reported as not
# assessed, never as a verdict (see _call_bedrock_json).
_BEDROCK_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10})

# Verdict for a use case the classifier could not assess (Bedrock error,
# refusal, truncated or unparseable output). Never counted as STOP/REFINE/EXPAND.
NOT_ASSESSED = "NOT_ASSESSED"
NOT_ASSESSED_ACTIVITY = "Not assessed (classification call failed)"

# requestMetadata keys that are not cost attribution: session ids, plus the
# keys the readers add themselves (OTLP source/service fields).
_NON_TAG_KEYS = frozenset(_SESSION_ID_KEYS) | {"source", "source_format", "service_name", "service_version"}

# Whole words only, so "transcript", "description" and "ruled" do not match.
_DETERMINISTIC_RE = re.compile(
    r"\b(?:deterministic|regex(?:es)?|scripts?|lookups?|without ai|rules?)\b")


def _bedrock_client(region):
    return boto3.client("bedrock-runtime", region_name=region, config=_BEDROCK_CONFIG)


def classify_activities(store, sessions, region, model_id, audit_nonce=None):
    """Pass 1: describe the business task behind each session (tool-agnostic).

    Sessions carry `invocation_ids`, not full invocation content (see
    cli._group_into_sessions), so each worker fetches its session's full rows
    from `store` right before describing it, classifies, then lets them go
    out of scope. At most PASS1_MAX_WORKERS sessions' full payloads are ever
    resident at once, not the whole window's, which is the property that
    keeps this bounded at org scale (see docs/ARCHITECTURE.md's
    memory-ceiling section).

    Sessions are independent (classifying one never depends on another), so this
    fans out across a thread pool instead of one call at a time. One
    Bedrock client per worker thread (thread-local), same pattern as the S3
    fetch in reader.py, not one client shared or recreated per session.
    """
    if not sessions:
        return []

    _local = threading.local()

    def _client():
        if not hasattr(_local, "bedrock"):
            _local.bedrock = _bedrock_client(region)
        return _local.bedrock

    def _describe(session):
        invocations = store.fetch_full_invocations(session["invocation_ids"])
        return _describe_activity(_client(), session, invocations, model_id, audit_nonce)

    max_workers = min(PASS1_MAX_WORKERS, len(sessions))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # pool.map preserves input order in its output, so activities line up
        # with the session list the caller passed in.
        return list(pool.map(_describe, sessions))


def cluster_use_cases(activities, region, model_id, audit_nonce=None):
    """Pass 2a: group activities into distinct use cases by meaning.

    Below CHUNK_SIZE this is exactly the original single-call path, unchanged.
    Above it, activities are split into chunks small enough to safely fit
    Pass 2a's max_tokens=3000 response budget (the size that is already known
    to work well, see docs/ARCHITECTURE.md's cost model), each chunk is
    clustered independently by the same _cluster_activities used below, then
    a second, much smaller pass merges same-meaning clusters across chunks.
    """
    bedrock = _bedrock_client(region)

    if len(activities) <= CHUNK_SIZE:
        return _check_partition(activities, _cluster_activities(bedrock, activities, model_id, audit_nonce), "Pass 2a")

    chunk_clusters = []
    for chunk in _chunk_list(activities, CHUNK_SIZE):
        chunk_clusters.extend(_cluster_activities(bedrock, chunk, model_id, audit_nonce))
    _check_partition(activities, chunk_clusters, "Pass 2a")

    merged = _merge_clusters_across_chunks(bedrock, chunk_clusters, model_id,
                                           audit_nonce=audit_nonce)
    return _check_partition(activities, merged, "Pass 2a merge")


def _check_partition(activities, clusters, stage):
    """Invariant: every activity lands in exactly one cluster, so total cost
    is unchanged. A model that lists an id twice must never double count."""
    placed = sorted(id(s) for c in clusters for s in c["sessions"])
    expected = sorted(id(a) for a in activities)
    before = sum(a["metrics"]["total_cost_usd"] for a in activities)
    after = sum(c["metrics"]["total_cost_usd"] for c in clusters)
    if placed != expected or abs(after - before) > 1e-6 * max(1.0, abs(before)):
        raise RuntimeError(f"{stage} changed the session set or total cost "
                           f"(${before:.4f} -> ${after:.4f}); refusing to report double-counted spend.")
    return clusters


def classify_use_cases(clusters, region, model_id, window_days=7, audit_nonce=None):
    """Pass 2b: STOP/REFINE/EXPAND + cost optimisation per use case.

    window_days is the observation window the logs cover; used to compute the
    monthly cost projection deterministically in Python (never by the LLM).
    """
    bedrock = _bedrock_client(region)
    return [_classify_use_case(bedrock, c, model_id, window_days, audit_nonce) for c in clusters]


def project_monthly_cost(observed_cost_usd, window_days):
    """Linear projection of observed spend to 30 days. Deterministic."""
    if window_days <= 0:
        return 0.0
    return observed_cost_usd / window_days * 30


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------

def _describe_activity(bedrock, session, invocations, model_id, audit_nonce=None):
    """Name the underlying business task for one session, ignoring the tool.

    `invocations` is the full-payload fetch for this session's ids (see
    classify_activities), not a field on `session`, sessions carry only
    invocation_ids now.
    """
    sample_user_messages = []
    sample_responses = []
    models_used = set()
    all_tools = set()
    tagged_count = 0        # invocations carrying cost-attribution requestMetadata
    guardrail_count = 0     # invocations with a max_tokens cap set
    unpriced_count = 0      # invocations whose model has no known rate (costed at $0)

    for inv in invocations:
        models_used.add(inv.get("model", "unknown"))
        for t in inv.get("tools", []):
            all_tools.add(t)
        md = inv.get("metadata")
        if isinstance(md, dict) and any(k not in _NON_TAG_KEYS for k in md):
            tagged_count += 1
        if inv.get("max_tokens") is not None:
            guardrail_count += 1
        # cost_priced is True for a table or source-reported rate (OTLP sets it
        # only for source-reported cost, so also check the table).
        if not inv.get("cost_priced") and not get_pricing(inv.get("model") or "unknown")[1]:
            unpriced_count += 1
        if len(sample_user_messages) < 10:
            for msg in inv.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    real_text = _extract_real_user_text(msg.get("content", ""))
                    if real_text and len(real_text) > 20:
                        sample_user_messages.append(real_text[:500])
                        break
        # The OUTPUT often reveals the value even when the input is messy
        # (e.g. a garbled meeting transcript that produces a structured brief).
        if len(sample_responses) < 3:
            resp = inv.get("response_text", "")
            if resp and len(resp) > 40:
                sample_responses.append(resp[:500])

    total_input = sum(inv.get("input_tokens", 0) for inv in invocations)
    total_output = sum(inv.get("output_tokens", 0) for inv in invocations)

    # Real agentic sessions bury the task in tool_results / assistant text /
    # AskUserQuestion payloads, not the user role. If the user-role scan came
    # up thin, pull richer signal from the whole session so Pass 1 isn't
    # classifying "(no clear user messages)" for a 200-invocation session.
    if len(sample_user_messages) < 3:
        sample_user_messages = extract_session_signal(invocations)

    nonce = make_nonce()
    user_block = "\n".join(f"- {m}" for m in sample_user_messages) if sample_user_messages else "(no clear user messages captured)"
    resp_block = "\n".join(f"- {r}" for r in sample_responses) if sample_responses else "(no responses captured)"

    prompt = _pass1.build_prompt(
        nonce=nonce, user_block=user_block, resp_block=resp_block,
        all_tools=all_tools, invocation_count=len(invocations),
        total_input=total_input, total_output=total_output,
    )

    parsed = _call_bedrock_json(bedrock, model_id, prompt, audit_nonce=audit_nonce)
    failed = parsed is None
    if failed:
        parsed = {"activity": NOT_ASSESSED_ACTIVITY}

    return {
        "session_id": session["session_id"],
        "classification_error": failed,
        "activity": _as_str(parsed.get("activity")).strip() or "Task not identifiable from logs",
        "description": _as_str(parsed.get("description")),
        "business_value": _as_str(parsed.get("business_value")),
        "confidence": _as_choice(parsed.get("confidence"), CONFIDENCES, "low"),
        "caller": session["caller"],
        "duration": _session_duration(session),
        "metrics": _session_metrics(session, invocations, models_used, tagged_count, guardrail_count,
                                    unpriced_count),
        "samples": _session_samples(invocations, max_samples=5),
    }


# ---------------------------------------------------------------------------
# Pass 2a
#
# One use_cases JSON entry (name + description + one or more ids) costs
# roughly 45-50 tokens in the response. max_tokens=3000 on _cluster_activities
# therefore only safely holds ~50-60 entries even in the worst case (a chunk
# that fails to dedup at all, one entry per input). CHUNK_SIZE and
# MERGE_MAX_CLUSTERS both use that same conservative bound, so a single call
# to either _cluster_activities or the merge step never has room to truncate.
# ---------------------------------------------------------------------------

CHUNK_SIZE = 50
MERGE_MAX_CLUSTERS = 50


def _chunk_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _cluster_activities(bedrock, activities, model_id, audit_nonce=None):
    """Group Pass-1 activities into distinct use cases by meaning."""
    # Nothing to cluster
    if not activities:
        return []

    nonce = make_nonce()
    listing = [
        untrusted_entry(a["session_id"], {"activity": a["activity"], "desc": a["description"]},
                        nonce, trusted=f'cost=${a["metrics"]["total_cost_usd"]:.2f}')
        for a in activities
    ]

    prompt = _pass2a.build_prompt(listing=listing)

    parsed = _call_bedrock_json(bedrock, model_id, prompt, max_tokens=3000, audit_nonce=audit_nonce) or {}
    use_cases = _as_dict_list(parsed.get("use_cases"))

    # Only ids this call issued are accepted; anything else the model returns is dropped.
    by_id = {a["session_id"]: a for a in activities}
    clusters = []
    assigned = set()

    for uc in use_cases:
        # First listing wins: an id repeated within or across use cases is
        # placed once, so its cost is counted once.
        ids = [sid for sid in dict.fromkeys(_as_str_list(uc.get("session_ids")))
               if sid in by_id and sid not in assigned]
        members = [by_id[sid] for sid in ids]
        if not members:
            continue
        assigned.update(ids)
        clusters.append(_build_cluster(_as_str(uc.get("name")).strip() or "Unnamed use case",
                                        _as_str(uc.get("description")), members))

    # Safety net: any activity the model dropped becomes its own use case
    orphans = [a for a in activities if a["session_id"] not in assigned]
    for a in orphans:
        clusters.append(_build_cluster(a["activity"], a["description"], [a]))

    return clusters


def _build_cluster(name, description, members):
    """Aggregate member activities into a use-case cluster with summed metrics."""
    total_cost = sum(m["metrics"]["total_cost_usd"] for m in members)
    total_input = sum(m["metrics"]["total_input_tokens"] for m in members)
    total_output = sum(m["metrics"]["total_output_tokens"] for m in members)
    total_invocations = sum(m["metrics"]["invocation_count"] for m in members)
    total_tagged = sum(m["metrics"].get("tagged_count", 0) for m in members)
    total_guardrail = sum(m["metrics"].get("guardrail_count", 0) for m in members)
    total_unpriced = sum(m["metrics"].get("unpriced_count", 0) for m in members)
    models = sorted({mdl for m in members for mdl in m["metrics"]["models_used"]})
    callers = sorted({m["caller"] for m in members})

    return {
        "name": name,
        "description": description,
        "sessions": members,
        "metrics": {
            "session_count": len(members),
            "invocation_count": total_invocations,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": total_cost,
            "models_used": models,
            "caller_count": len(callers),
            "tagged_count": total_tagged,
            "guardrail_count": total_guardrail,
            "unpriced_count": total_unpriced,
        },
    }


def cap_use_cases(clusters, max_use_cases):
    """Deterministically bound the final use-case count in Python.

    Merge convergence at enterprise scale is not guaranteed (see
    MERGE_MAX_DEPTH below: a sanity backstop, not a real limit), so without
    this, a bad merge outcome could return hundreds of use cases and blow out
    both the report and the Pass 2b bill it drives. Applied before Pass 2b so
    it bounds cost, not just report size.

    Kept in cost-descending order (real orgs concentrate spend in a handful
    of use cases, see docs/ARCHITECTURE.md's cost model); anything past
    max_use_cases is rolled into one "Other" cluster so it still counts
    toward totals without a per-item Pass 2b call. Set max_use_cases well
    above any customer's self-reported use-case count (e.g. ~3x) - the cost
    of a wider cap is cents (see docs/ARCHITECTURE.md's cost model), the cost
    of too narrow a cap is silently losing a real, distinct use case into
    "Other".
    """
    if len(clusters) <= max_use_cases:
        return clusters
    sessions = [s for c in clusters for s in c["sessions"]]

    ranked = sorted(clusters, key=lambda c: c["metrics"]["total_cost_usd"], reverse=True)
    kept, overflow = ranked[:max_use_cases], ranked[max_use_cases:]

    overflow_members = [s for c in overflow for s in c["sessions"]]
    other = _build_cluster(
        "Other (long tail)",
        f"{len(overflow)} lower-cost use cases below the top {max_use_cases}, "
        "grouped here to bound report size and cost. Raise --max-use-cases "
        "to see them individually.",
        overflow_members,
    )
    return _check_partition(sessions, kept + [other], "Use-case cap")


MERGE_MAX_DEPTH = 6  # log_50(huge number) is small; this is a sanity backstop, not a real limit.


def _merge_clusters_across_chunks(bedrock, chunk_clusters, model_id, depth=0, audit_nonce=None):
    """Merge same-meaning clusters produced independently by different chunks.

    Recursive, not a one-shot with a hard ceiling: if there are more clusters
    than one merge call can safely describe without truncating a response
    (see CHUNK_SIZE's comment above), clusters are grouped into batches of
    MERGE_MAX_CLUSTERS, each batch is merged independently by
    _merge_one_batch (the same call this function always made), and the
    *results* of those batches are merged again, recursively, until they fit
    in a single call. This is the same chunk-then-merge shape cluster_use_cases
    already uses for Pass 2a itself, just applied one level up and repeated
    until it converges.

    Convergence is guaranteed: each level's merge can only produce as many
    or fewer clusters than it was given (every input cluster ends up in
    exactly one output group, orphans included), so the count strictly
    shrinks or a level fully merges everything into groups, either way it
    reaches <= MERGE_MAX_CLUSTERS in a bounded number of levels for any
    realistic cluster count. depth is a sanity backstop, not a real limit,
    if it is ever hit something is wrong (e.g. a pathological input where
    every cluster refuses to merge with anything, at every level) and
    returning the clusters as-is (no more merged than already achieved) is
    safer than looping forever.
    """
    if len(chunk_clusters) <= 1:
        return chunk_clusters

    if len(chunk_clusters) <= MERGE_MAX_CLUSTERS:
        return _merge_one_batch(bedrock, chunk_clusters, model_id, audit_nonce)

    if depth >= MERGE_MAX_DEPTH:
        return chunk_clusters

    batches = _chunk_list(chunk_clusters, MERGE_MAX_CLUSTERS)
    merged_batches = []
    for batch in batches:
        merged_batches.extend(_merge_one_batch(bedrock, batch, model_id, audit_nonce))

    return _merge_clusters_across_chunks(bedrock, merged_batches, model_id, depth=depth + 1, audit_nonce=audit_nonce)


def _merge_one_batch(bedrock, chunk_clusters, model_id, audit_nonce=None):
    """Merge same-meaning clusters within a single batch (one Bedrock call).

    Reuses _build_cluster (not new aggregation logic): a merge group's members
    are the underlying activities from each merged cluster's "sessions" list,
    flattened, so the merged cluster's metrics are computed the exact same way
    a single-call Pass 2a cluster's metrics always have been. Caller
    (_merge_clusters_across_chunks) guarantees len(chunk_clusters) is within
    the safe response-size bound, this function never checks or degrades.
    """
    nonce = make_nonce()
    listing = [
        untrusted_entry(f"cluster_{i}", {"name": c["name"], "desc": c["description"]}, nonce,
                        trusted=(f'sessions={c["metrics"]["session_count"]} '
                                 f'cost=${c["metrics"]["total_cost_usd"]:.2f}'))
        for i, c in enumerate(chunk_clusters)
    ]

    prompt = _pass2a_merge.build_prompt(listing=listing)

    parsed = _call_bedrock_json(bedrock, model_id, prompt, max_tokens=3000, audit_nonce=audit_nonce) or {}
    merged = _as_dict_list(parsed.get("merged"))

    # Only ids this call issued are accepted; anything else the model returns is dropped.
    by_id = {f"cluster_{i}": c for i, c in enumerate(chunk_clusters)}
    result = []
    assigned = set()

    for group in merged:
        ids = [cid for cid in dict.fromkeys(_as_str_list(group.get("cluster_ids")))
               if cid in by_id and cid not in assigned]
        member_clusters = [by_id[cid] for cid in ids]
        if not member_clusters:
            continue
        assigned.update(ids)
        flattened_members = [s for c in member_clusters for s in c["sessions"]]
        result.append(_build_cluster(
            _as_str(group.get("name")).strip() or member_clusters[0]["name"],
            _as_str(group.get("description")) or member_clusters[0]["description"],
            flattened_members,
        ))

    # Safety net: any cluster the merge step dropped stays as its own use
    # case, same pattern as _cluster_activities's orphan handling.
    orphans = [c for i, c in enumerate(chunk_clusters) if f"cluster_{i}" not in assigned]
    result.extend(orphans)

    return result


# ---------------------------------------------------------------------------
# Pass 2b
# ---------------------------------------------------------------------------

def _classify_use_case(bedrock, cluster, model_id, window_days=7, audit_nonce=None):
    """STOP/REFINE/EXPAND + cost optimisation for one use case."""
    m = cluster["metrics"]
    sample_activities = "\n".join(
        f'- {s["activity"]}: {s["description"]}' for s in cluster["sessions"][:8]
    )

    # Compute the projection in Python. The LLM only annotates it - it must
    # never do the arithmetic (it invents numbers).
    projected_monthly = project_monthly_cost(m["total_cost_usd"], window_days)

    prompt = _pass2b.build_prompt(
        nonce=make_nonce(), cluster_name=cluster['name'], cluster_description=cluster['description'],
        sample_activities=sample_activities, metrics=m,
        projected_monthly=projected_monthly, window_days=window_days,
    )

    # Every session failed Pass 1: nothing to assess, so no Pass 2b call.
    sessions = cluster["sessions"]
    if sessions and all(s.get("classification_error") for s in sessions):
        return _not_assessed(cluster, projected_monthly, window_days,
                             f"Pass 1 failed for all {len(sessions)} session(s).")

    parsed = _call_bedrock_json(bedrock, model_id, prompt, audit_nonce=audit_nonce)
    if parsed is None:
        return _not_assessed(cluster, projected_monthly, window_days, "Pass 2b classification call failed.")
    parsed = _normalise_pass2b(parsed)

    # Objective checks computed in Python from log facts (not LLM prose).
    cost_optimizations = parsed["cost_optimizations"]
    cost_optimizations["tagging_compliance"] = _check_tagging(m)
    cost_optimizations["guardrails"] = _check_guardrails(m)

    # Apply the deterministic verdict rubric: the LLM proposes, but STOP and
    # EXPAND must clear evidence-based bars computed from log facts. Keeps us
    # from saying STOP on a business-value judgement we can't make, and from
    # rubber-stamping everything as REFINE.
    recommendation, verdict_reason = _apply_verdict_rubric(
        parsed["recommendation"], cluster, cost_optimizations, parsed
    )

    return {
        "name": cluster["name"],
        "description": cluster["description"],
        "recommendation": recommendation,
        "verdict_reason": verdict_reason,
        "category": parsed["category"],
        "nature": parsed["nature"],
        "nature_reasoning": parsed["nature_reasoning"],
        "reasoning": parsed["reasoning"],
        "example_tasks": parsed["example_tasks"],
        "refinement_suggestions": parsed["refinement_suggestions"],
        "cost_optimizations": cost_optimizations,
        "projected_monthly_cost_usd": projected_monthly,
        "estimated_monthly_projection": _format_projection(projected_monthly, window_days,
                                                           parsed["projection_note"],
                                                           m.get("unpriced_count", 0)),
        "metrics": cluster["metrics"],
        "sessions": cluster["sessions"],
    }


def _not_assessed(cluster, projected_monthly, window_days, reason):
    """Assessment record for a use case the classifier could not assess."""
    m = cluster["metrics"]
    return {
        "name": cluster["name"],
        "description": cluster["description"],
        "recommendation": NOT_ASSESSED,
        "verdict_reason": f"{reason} Re-run to assess this use case.",
        "category": "non_coding",
        "nature": "experimental",
        "nature_reasoning": "",
        "reasoning": "",
        "example_tasks": [],
        "refinement_suggestions": [],
        "cost_optimizations": {"tagging_compliance": _check_tagging(m), "guardrails": _check_guardrails(m)},
        "projected_monthly_cost_usd": projected_monthly,
        "estimated_monthly_projection": _format_projection(projected_monthly, window_days, "",
                                                           m.get("unpriced_count", 0)),
        "metrics": m,
        "sessions": cluster["sessions"],
    }


def _normalise_pass2b(parsed):
    """Pass 2b model JSON -> fixed shape with safe types and allowlisted enums."""
    return {
        "recommendation": _as_choice(parsed.get("recommendation"), RECOMMENDATIONS, "refine").upper(),
        "category": _as_choice(parsed.get("category"), CATEGORIES, "non_coding"),
        "nature": _as_choice(parsed.get("nature"), NATURES, "experimental"),
        "nature_reasoning": _as_str(parsed.get("nature_reasoning")),
        "reasoning": _as_str(parsed.get("reasoning")),
        "example_tasks": _as_str_list(parsed.get("example_tasks")),
        "refinement_suggestions": _as_str_list(parsed.get("refinement_suggestions")),
        "cost_optimizations": _as_checks(parsed.get("cost_optimizations")),
        "projection_note": _as_str(parsed.get("projection_note")),
    }


# ---------------------------------------------------------------------------
# Verdict rubric
#
# The LLM proposes a recommendation, but STOP and EXPAND must clear
# evidence-based bars we can actually defend from log facts. This is
# deliberately simple and readable (it's a demo, and the accuracy is not yet
# eval-validated - see the rules file). Two axes, not one:
#   - EVIDENCE (provable from logs) gates STOP.
#   - EFFICIENCY (opportunity) is where REFINE/EXPAND live.
# We never STOP something merely inefficient - that is REFINE. We only STOP
# when the logs themselves show there is little there to keep paying for.
# ---------------------------------------------------------------------------

def _apply_verdict_rubric(llm_reco, cluster, cost_opts, parsed):
    """Return (recommendation, reason) after applying deterministic bars.

    STOP if a log-provable signal fires; EXPAND only if it clears every bar;
    otherwise REFINE (the honest default), with the reason naming why.
    """
    m = cluster["metrics"]
    sessions = cluster.get("sessions", [])

    # --- STOP: evidence-based, log-provable only -------------------------
    # 1. Abandoned / no coherent output: every activity came back unidentifiable.
    #    Sessions whose Pass 1 call failed are not evidence either way.
    assessed = [s for s in sessions if not s.get("classification_error")]
    identifiable = [s for s in assessed
                    if "not identifiable" not in (s.get("activity", "").lower())]
    if assessed and not identifiable:
        return "STOP", ("No identifiable task or usable output across "
                        f"{len(assessed)} session(s) - looks abandoned or broken.")

    # 2. Provably deterministic work the LLM itself flagged (a rule/script fits).
    #    The model signals this by proposing STOP with a deterministic rationale.
    reason_txt = (parsed.get("reasoning", "") or "").lower()
    if llm_reco == "STOP" and _DETERMINISTIC_RE.search(reason_txt):
        return "STOP", "Task looks deterministic - a script/rule would be cheaper and more reliable than a model."

    # 3. Negligible + stale: trivial spend and a single one-off session.
    #    Spend is not known to be trivial if any invocation is unpriced ($0).
    if (parsed.get("nature") == "experimental"
            and m.get("session_count", 0) <= 1
            and m.get("unpriced_count", 0) == 0
            and m.get("total_cost_usd", 0.0) < 1.0):
        return "STOP", ("Negligible one-off (single session, trivial spend) - "
                        "not worth managing.")

    # --- EXPAND: must clear EVERY bar ------------------------------------
    has_fail = any((c or {}).get("status") == "fail" for c in cost_opts.values())
    high_confidence = any(s.get("confidence") == "high" for s in sessions)
    repeatable_at_scale = (parsed.get("nature") == "repeatable"
                           and m.get("invocation_count", 0) >= 10)
    # nature, confidence and llm_reco all come from model output that logged
    # (untrusted) prompts can steer, so EXPAND also needs recurrence the logs
    # prove: the work showed up in several separate sessions.
    recurs_in_logs = m.get("session_count", 0) >= EXPAND_MIN_SESSIONS
    if ((not has_fail) and high_confidence and repeatable_at_scale and recurs_in_logs
            and llm_reco == "EXPAND"):
        return "EXPAND", ("Clear value, efficient, and a recurring workflow at scale - "
                          "worth scaling.")

    # --- REFINE: the honest default, name the reason ---------------------
    fails = [k for k, c in cost_opts.items() if (c or {}).get("status") == "fail"]
    if fails:
        return "REFINE", f"Real value but has open cost findings: {', '.join(fails)}."
    if llm_reco == "EXPAND":
        return "REFINE", ("Valuable, but does not yet clear the EXPAND bar "
                          "(needs high-confidence value + recurring scale across "
                          f"{EXPAND_MIN_SESSIONS}+ sessions + no open findings).")
    return "REFINE", "Real value with room to improve efficiency."


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _check_tagging(metrics):
    """Objective: is requestMetadata populated for cost attribution?"""
    total = metrics["invocation_count"]
    tagged = metrics.get("tagged_count", 0)
    if total == 0:
        return {"status": "warn", "detail": "No invocations to assess."}
    if tagged == 0:
        return {"status": "fail",
                "detail": "No invocations carry requestMetadata. Add team/project/environment tags for cost attribution."}
    if tagged < total:
        return {"status": "warn",
                "detail": f"Only {tagged}/{total} invocations carry requestMetadata. Tag consistently for full cost attribution."}
    return {"status": "pass",
            "detail": f"All {total} invocations carry requestMetadata for cost attribution."}


def _check_guardrails(metrics):
    """Objective: is a max_tokens output cap set on requests?"""
    total = metrics["invocation_count"]
    capped = metrics.get("guardrail_count", 0)
    if total == 0:
        return {"status": "warn", "detail": "No invocations to assess."}
    if capped == 0:
        return {"status": "fail",
                "detail": "No invocations set a max_tokens output cap. Add one to prevent runaway generation."}
    if capped < total:
        return {"status": "warn",
                "detail": f"Only {capped}/{total} invocations set a max_tokens cap."}
    return {"status": "pass",
            "detail": f"All {total} invocations set a max_tokens output cap."}


def _format_projection(projected_monthly, window_days, note, unpriced_count=0):
    """Build the projection string from the deterministic number + LLM note."""
    base = f"~${projected_monthly:.2f}/month (linear projection of observed spend over {window_days} day(s))"
    if unpriced_count:
        base += (f". Partial: {unpriced_count} invocation(s) use models with no known rate "
                 "and are counted as $0")
    note = (note or "").strip()
    if note:
        return f"{base}. {note}"
    return base


def _session_duration(session):
    if session.get("first_ts") and session.get("last_ts"):
        mins = (session["last_ts"] - session["first_ts"]).total_seconds() / 60
        return f"{mins:.0f} minutes"
    return ""


def _session_metrics(session, invocations, models_used, tagged_count=0, guardrail_count=0,
                     unpriced_count=0):
    return {
        "invocation_count": len(invocations),
        "total_input_tokens": sum(inv.get("input_tokens", 0) for inv in invocations),
        "total_output_tokens": sum(inv.get("output_tokens", 0) for inv in invocations),
        "total_cost_usd": session["total_cost_usd"],
        "models_used": list(models_used),
        "tagged_count": tagged_count,
        "guardrail_count": guardrail_count,
        "unpriced_count": unpriced_count,
    }


def _session_samples(invocations, max_samples=5):
    """Representative prompt/response samples for technical drill-down."""
    samples = []
    for inv in invocations:
        if len(samples) >= max_samples:
            break

        user_msg = ""
        for msg in inv.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                real_text = _extract_real_user_text(msg.get("content", ""))
                if real_text and len(real_text) > 20:
                    user_msg = real_text[:800]
                    break

        if not user_msg:
            continue

        samples.append({
            "timestamp": inv.get("timestamp", ""),
            "user_message": user_msg,
            "response_preview": inv.get("response_text", "")[:800],
            "input_tokens": inv.get("input_tokens", 0),
            "output_tokens": inv.get("output_tokens", 0),
            "model": inv.get("model", "unknown"),
        })

    return samples


def extract_session_signal(invocations, max_items=12, max_chars=500):
    """Best-effort human/business signal for a whole agentic session.

    Real Claude-Code / agent sessions rarely put the task in a clean user
    message. The turn structure is tool_result -> assistant(text+tool_use) ->
    tool_result..., and the original human ask often scrolled out of the logged
    message window. So looking only at the user role (see
    _extract_real_user_text) returns nothing for big sessions.

    This pulls signal from EVERYWHERE it actually lives, in priority order:
      1. Genuine user-typed text (not tool_result / system-reminder).
      2. AskUserQuestion inputs and their answers (explicit intent).
      3. Assistant natural-language `text` blocks (what it said it was doing).
      4. tool_use inputs for meaningful tools (Write/Edit/Bash commands, etc).
    Framework noise (tool schemas, deferred-tool dumps, system-reminders) is
    filtered. Returns a de-duplicated list of short snippets, most-signal first.
    """
    seen = set()
    user_typed, questions, assistant_text, tool_actions = [], [], [], []

    def _add(bucket, text):
        if not text:
            return
        text = " ".join(str(text).split())
        if len(text) < 15:
            return
        key = text[:80]
        if key in seen:
            return
        seen.add(key)
        bucket.append(text[:max_chars])

    for inv in invocations:
        for msg in inv.get("messages", []):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type", "text" if "text" in b else None)

                if btype == "text":
                    text = b.get("text", "")
                    if not text or text.strip().startswith("<system-reminder>"):
                        continue
                    if "deferred tools are now available" in text[:200]:
                        continue
                    if role == "user":
                        _add(user_typed, text)
                    elif role == "assistant":
                        _add(assistant_text, text)

                elif btype == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {}) or {}
                    if name == "AskUserQuestion":
                        for q in (inp.get("questions") or []):
                            if isinstance(q, dict):
                                _add(questions, q.get("question", ""))
                    elif name in ("Write", "Edit"):
                        _add(tool_actions, f"{name}: {inp.get('file_path', '')}")
                    elif name == "Bash":
                        _add(tool_actions, f"Bash: {inp.get('command', '')}")
                    elif name in ("Task", "Agent"):
                        _add(tool_actions, f"{name}: {inp.get('description') or inp.get('prompt', '')}")

                elif btype == "tool_result":
                    # tool_result may carry an AskUserQuestion ANSWER string.
                    c = b.get("content", "")
                    if isinstance(c, str) and c.startswith("Your questions have been answered"):
                        _add(questions, c)

    # Priority order: explicit human intent first, then what the agent did.
    combined = user_typed + questions + assistant_text + tool_actions
    return combined[:max_items]


def _extract_real_user_text(content):
    """Extract actual user-written text, filtering framework injections.

    Strips system-reminder blocks and tool schema dumps to find what the
    human actually typed.
    """
    if isinstance(content, str):
        if content.strip().startswith("<system-reminder>"):
            return ""
        return content

    if isinstance(content, list):
        real_parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if not text:
                continue
            if text.strip().startswith("<system-reminder>"):
                continue
            if "deferred tools are now available" in text[:200]:
                continue
            real_parts.append(text)
        return " ".join(real_parts)

    return ""


_usage_lock = threading.Lock()
_usage_by_run = {}


def audit_usage(audit_nonce):
    """Bedrock calls this run has made and their cost, shaped like use case metrics."""
    with _usage_lock:
        return dict(_usage_by_run.get(audit_nonce, _empty_usage()))


def _empty_usage():
    return {"invocation_count": 0, "total_cost_usd": 0.0, "unpriced_count": 0}


def _record_usage(audit_nonce, model_id, usage):
    cost, priced = estimate_cost(usage.get("inputTokens", 0), usage.get("outputTokens", 0), model_id,
                                 usage.get("cacheReadInputTokens", 0), usage.get("cacheWriteInputTokens", 0))
    with _usage_lock:
        totals = _usage_by_run.setdefault(audit_nonce, _empty_usage())
        totals["invocation_count"] += 1
        totals["total_cost_usd"] += cost
        totals["unpriced_count"] += 0 if priced else 1


def _call_bedrock_json(bedrock, model_id, prompt, max_tokens=2000, audit_nonce=None):
    """Invoke Bedrock and parse a JSON object response.

    Returns None on failure (API error after retries, truncated output,
    refusal or prose that is not a JSON object), so callers can tell a failed
    call apart from a real but empty answer.

    Every call is tagged as audit overhead. Only calls carrying this run's
    audit_nonce are later excluded from the audit (see AuditStore.issue_audit_nonce).
    """
    try:
        request_metadata = {"source": AUDIT_SOURCE}
        if audit_nonce:
            request_metadata["aiva_run"] = audit_nonce
        kwargs = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
            "requestMetadata": request_metadata,
        }
        response = bedrock.converse(**kwargs)
        # Billed even when the reply is truncated or unusable.
        _record_usage(audit_nonce, model_id, response.get("usage") or {})
        if response.get("stopReason") == "max_tokens":
            return None
        text = response["output"]["message"]["content"][0]["text"]
        parsed = _parse_json_response(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json_response(text):
    """Extract a JSON object from a model response. Returns None if none is found.

    Tries, in order: the whole reply, the first top-level object that decodes
    from any '{' (handles prose before the JSON and ``` inside string values),
    then a fenced code block. A '{' with unclosed braces before it is nested
    inside an object that did not decode (e.g. a reply truncated at
    max_tokens) and is skipped, so a fragment is never mistaken for the reply.
    """
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        if text.count("{", 0, start) <= text.count("}", 0, start):
            try:
                parsed, _ = decoder.raw_decode(text, start)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        start = text.find("{", start + 1)

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Type normalisation for model JSON. Model output is untrusted: any field may
# come back null, a list, a number or a dict. Normalise at the parse boundary
# so nothing downstream (rubric, reporters) has to guess.
# ---------------------------------------------------------------------------

def _as_str(value, default=""):
    return value if isinstance(value, str) else default


def _as_choice(value, choices, default):
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if value in choices else default


def _as_str_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _as_dict_list(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _as_checks(value):
    """cost_optimizations -> {key: {"status": pass|warn|fail, "detail": str}}."""
    if not isinstance(value, dict):
        return {}
    checks = {}
    for key, check in value.items():
        if isinstance(check, dict):
            checks[key] = {"status": _as_choice(check.get("status"), CHECK_STATUSES, "warn"),
                           "detail": _as_str(check.get("detail"))}
        elif isinstance(check, str):
            checks[key] = {"status": "warn", "detail": check}
    return checks
