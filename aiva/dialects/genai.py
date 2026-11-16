"""OpenTelemetry GenAI semantic conventions (gen_ai.*), and LLM events from
other instrumentation that carry a model and token counts. The fallback
dialect: any event it does not match is dropped."""

from aiva.dialects import common


def matches(event):
    attrs = event.attrs
    return bool(event.name.startswith(("gen_ai.", "llm."))
                or attrs.get("gen_ai.usage.input_tokens") or attrs.get("gen_ai.request.model")
                or (attrs.get("input_tokens") and attrs.get("model")))


def to_record(event):
    return common.invocation(event, use_body=not event.is_span)
