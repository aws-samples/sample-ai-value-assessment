"""Registry of log sources. The CLI and preflight look a source up here
instead of switching on its name; adding a source is one entry."""

from dataclasses import dataclass
from types import ModuleType

from aiva import otlp_reader, reader


@dataclass(frozen=True)
class Source:
    name: str
    label: str        # shown in the run header
    reading: str      # "Reading <reading> from S3..."
    present_check: str
    empty_hint: str
    adapter: ModuleType  # log_prefix, list_prefixes, wants_key, parse

    def log_prefix(self, prefix):
        return self.adapter.log_prefix(prefix)

    def list_prefixes(self, s3, bucket, base, cutoff):
        return self.adapter.list_prefixes(s3, bucket, base, cutoff)

    def wants_key(self, relative_key):
        return self.adapter.wants_key(relative_key)

    def parse(self, document, fetch_body):
        return self.adapter.parse(document, fetch_body)


SOURCES = {s.name: s for s in (
    Source(
        name="bedrock",
        label="Bedrock Model Invocation Logs",
        reading="invocation logs",
        present_check="Model Invocation Logs present",
        empty_hint=("This is expected on a brand-new setup. Logging takes a few minutes to "
                    "start appearing after the first Bedrock call; check --prefix if you "
                    "expected data immediately."),
        adapter=reader,
    ),
    Source(
        name="otlp",
        label="OTLP Telemetry",
        reading="OTLP telemetry logs",
        present_check="OTLP logs present",
        empty_hint=("This is expected on a brand-new setup. Confirm your OpenTelemetry "
                    "Collector is writing to this prefix; check --prefix if you expected "
                    "data immediately."),
        adapter=otlp_reader,
    ),
)}
