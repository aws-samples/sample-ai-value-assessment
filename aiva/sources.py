"""Registry of log sources. The CLI, config and preflight look a source up
here instead of switching on its name; adding a source is one adapter module
with a SOURCE, and one entry below."""

from aiva import otlp_reader, reader

SOURCES = {s.name: s for s in (reader.SOURCE, otlp_reader.SOURCE)}
