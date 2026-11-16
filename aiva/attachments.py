"""Attachments layer: puts Attachments on the invocations they belong to.

Some sources report a call's context (prompt text, response text, tools) as
separate events, which can land in different S3 objects from the call. The
store keeps them as Attachments; this module is the only place that knows
how they join, so the store stays a plain record store and the classifier
only ever sees invocations with their context filled in.
"""


def load_invocations(store, ids):
    """Full invocations for `ids` (see AuditStore.fetch_full_invocations),
    with their attachments applied."""
    invocations = store.fetch_full_invocations(ids)
    first_call = {}
    for inv in invocations:
        prompt_id = inv["metadata"].get("prompt_id")
        if prompt_id and prompt_id not in first_call:
            first_call[prompt_id] = inv
    if first_call:
        attach(invocations, first_call, store.fetch_attachments(list(first_call)))
    return invocations


def attach(invocations, first_call, attachments):
    """Put each prompt's text and tool names on its first call, and each
    response on the call with its request_id (else the first call)."""
    by_request = {inv["request_id"]: inv for inv in invocations if inv["request_id"]}
    for a in attachments:
        call = first_call[a.prompt_id]
        if a.kind == "prompt" and not call["messages"]:
            call["messages"] = [{"role": "user", "content": a.text}]
        elif a.kind == "tool" and a.text not in call["tools"]:
            call["tools"].append(a.text)
        elif a.kind == "response":
            call = by_request.get(a.request_id, call)
            if not call["response_text"]:
                call["response_text"] = a.text
