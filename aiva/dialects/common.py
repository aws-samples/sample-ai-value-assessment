"""Attribute reading shared by the dialects: model, tokens, caller, session
and metadata. Accepts both vendor-native names (input_tokens) and the GenAI
convention names (gen_ai.usage.input_tokens)."""

from aiva.invocation import Invocation, as_int


def invocation(event, use_body=False):
    """An Invocation from an event's attributes. `use_body` falls back to the
    log body as the user message, for dialects whose body is content."""
    attrs, resource_attrs = event.attrs, event.resource_attrs
    model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = _model_and_tokens(attrs, resource_attrs)
    session_key = attrs.get("session.id") or resource_attrs.get("session.id") or ""

    messages = []
    user_prompt = attrs.get("prompt", attrs.get("gen_ai.prompt", ""))
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    elif use_body and event.body:
        messages.append({"role": "user", "content": event.body})

    service_name = resource_attrs.get("service.name", "")
    return Invocation(
        timestamp=event.timestamp,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        system_prompt=attrs.get("gen_ai.system_prompt", ""),
        tools=_list_attr(attrs, "tools"),
        messages=messages,
        response_text=attrs.get("response", attrs.get("gen_ai.completion", "")),
        max_tokens=attrs.get("gen_ai.request.max_tokens"),
        caller=_caller(attrs, resource_attrs),
        user_agent=service_name,
        operation=event.name or ("otlp_span" if event.is_span else "otlp_log"),
        request_id=attrs.get("request_id", attrs.get("gen_ai.request.id", "")),
        metadata=_metadata(attrs, resource_attrs, session_key),
    )


def _model_and_tokens(attrs, resource_attrs):
    model = (attrs.get("model")
             or attrs.get("gen_ai.request.model")
             or attrs.get("gen_ai.response.model")
             or resource_attrs.get("gen_ai.request.model")
             or "unknown")
    input_tokens = as_int(attrs.get("input_tokens",
                                    attrs.get("gen_ai.usage.input_tokens", 0)))
    output_tokens = as_int(attrs.get("output_tokens",
                                     attrs.get("gen_ai.usage.output_tokens", 0)))
    cache_read_tokens = as_int(attrs.get("cache_read_tokens",
                                         attrs.get("gen_ai.usage.cache_read_input_tokens", 0)))
    cache_write_tokens = as_int(attrs.get("cache_creation_tokens",
                                          attrs.get("gen_ai.usage.cache_creation_input_tokens", 0)))
    return model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens


def _caller(attrs, resource_attrs):
    return (attrs.get("user.email")
            or attrs.get("user.id")
            or resource_attrs.get("enduser.id")
            or resource_attrs.get("user.email")
            or "unknown")


def _metadata(attrs, resource_attrs, session_key):
    """The metadata dict that store.py uses for session_key extraction."""
    metadata = {}
    if session_key:
        metadata["session_id"] = session_key
    metadata["source_format"] = "otlp"
    metadata["service_name"] = resource_attrs.get("service.name", "")
    if resource_attrs.get("service.version"):
        metadata["service_version"] = resource_attrs["service.version"]
    if attrs.get("deployment.environment"):
        metadata["environment"] = attrs["deployment.environment"]
    if resource_attrs.get("deployment.environment"):
        metadata["environment"] = resource_attrs["deployment.environment"]
    return metadata


def _list_attr(attrs, key):
    val = attrs.get(key, [])
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        return [val]
    return []
