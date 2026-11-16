"""Claude Code and Cowork telemetry (https://code.claude.com/docs/en/monitoring-usage).

Cowork sends the same events as Claude Code, with extra resource attributes.
One prompt produces a stream of events tied together by session.id and
prompt.id: user_prompt, api_request, tool_decision, tool_result,
assistant_response, plus hooks, plugin and MCP events. Only api_request and
api_error are model calls. The prompt text, response text and tool names
become Attachments; every other event is dropped.

The log body is just the event name, so it is never used as content.
"""

from aiva.dialects import common
from aiva.invocation import Attachment

SERVICE_NAMES = {"claude-code", "cowork"}
CALL_EVENTS = {"api_request", "api_error"}
ATTACHMENT_EVENTS = {"user_prompt": "prompt", "assistant_response": "response",
                     "tool_decision": "tool", "tool_result": "tool"}
_ATTACHMENT_TEXT = {"prompt": "prompt", "response": "response", "tool": "tool_name"}
CALL_SPAN = "claude_code.llm_request"


def matches(event):
    """Claude Code / Cowork events, matched by event name first since
    OTEL_SERVICE_NAME can be changed."""
    name = _short_name(event.name)
    return (name in CALL_EVENTS or name in ATTACHMENT_EVENTS
            or event.name.startswith("claude_code.")
            or event.resource_attrs.get("service.name") in SERVICE_NAMES)


def to_record(event):
    name = _short_name(event.name)
    if name in ATTACHMENT_EVENTS and not event.is_span:
        return _attachment(ATTACHMENT_EVENTS[name], event)
    if name in CALL_EVENTS or (event.is_span and event.name == CALL_SPAN):
        record = common.invocation(event)
        if event.attrs.get("prompt.id"):
            record.metadata["prompt_id"] = str(event.attrs["prompt.id"])
        return record
    return None


def _attachment(kind, event):
    """None when there is no prompt.id to join on or nothing to carry, for
    example a user_prompt with prompt logging off."""
    prompt_id = event.attrs.get("prompt.id")
    text = event.attrs.get(_ATTACHMENT_TEXT[kind])
    if not prompt_id or not text:
        return None
    return Attachment(prompt_id=str(prompt_id), kind=kind, text=str(text),
                      request_id=str(event.attrs.get("request_id", "")), timestamp=event.timestamp)


def _short_name(name):
    return name.removeprefix("claude_code.")
