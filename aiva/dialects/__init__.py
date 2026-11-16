"""OpenTelemetry dialects: one module per telemetry schema, mapping decoded
OtelEvents (aiva/otlp_reader.py) onto the data model (aiva/invocation.py).

Each module provides matches(event) -> bool and to_record(event) ->
Invocation | Attachment | None. The first matching dialect owns the event,
including the decision to drop it, so the order matters: specific schemas
first, the generic GenAI conventions last. Adding a schema is one module and
one entry below.
"""

from aiva.dialects import claude_code, genai

DIALECTS = (claude_code, genai)
