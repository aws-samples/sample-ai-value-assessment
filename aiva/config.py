"""Run configuration: which log sources to read, and how to report.

`aiva run --config aiva.toml` reads it from a file; `aiva audit` builds the
same object from its flags. See aiva.example.toml.
"""

import tomllib
from dataclasses import dataclass, field

from aiva.sources import SOURCES

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"
FORMATS = ["html", "md", "json", "both", "all"]


@dataclass
class SourceConfig:
    type: str
    bucket: str
    prefix: str
    region: str


@dataclass
class RunConfig:
    region: str  # where aiva calls Bedrock to classify
    sources: list = field(default_factory=list)
    days: int = 7
    model: str = DEFAULT_MODEL
    output: str = "report"  # path without extension, local or s3://bucket/key
    format: str = "all"
    max_use_cases: int = 50
    show_samples: bool = False


_RUN_KEYS = {"region", "days", "model", "output", "format", "max_use_cases", "show_samples", "sources"}
_SOURCE_KEYS = {"type", "bucket", "prefix", "region"}


def load_config(path):
    """Read and check a TOML config file. Raises ValueError with a readable message."""
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from e

    unknown = set(raw) - _RUN_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    if "region" not in raw:
        raise ValueError(f"{path}: 'region' is required (the region aiva calls Bedrock in)")
    if raw.get("format", "all") not in FORMATS:
        raise ValueError(f"{path}: 'format' must be one of: {', '.join(FORMATS)}")
    if not raw.get("sources"):
        raise ValueError(f"{path}: add at least one [[sources]] entry")

    sources = []
    for i, src in enumerate(raw["sources"], start=1):
        unknown = set(src) - _SOURCE_KEYS
        if unknown:
            raise ValueError(f"{path}: source {i}: unknown setting(s): {', '.join(sorted(unknown))}")
        if src.get("type") not in SOURCES:
            raise ValueError(f"{path}: source {i}: 'type' must be one of: {', '.join(SOURCES)}")
        if not src.get("bucket"):
            raise ValueError(f"{path}: source {i}: 'bucket' is required")
        sources.append(SourceConfig(type=src["type"], bucket=src["bucket"],
                                    prefix=src.get("prefix", ""), region=src.get("region", raw["region"])))

    settings = {k: v for k, v in raw.items() if k != "sources"}
    return RunConfig(sources=sources, **settings)
