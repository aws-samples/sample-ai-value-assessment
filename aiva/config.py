"""Run configuration: which log sources to read, and how to report.

`aiva run --config aiva.toml` reads it from a file; `aiva audit` builds the
same object from its flags. See aiva.example.toml.
"""

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import boto3
import botocore.session
from botocore.credentials import RefreshableCredentials

from aiva.sources import SOURCES

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"
FORMATS = ["html", "md", "json", "both", "all"]


@dataclass
class SourceConfig:
    type: str
    bucket: str
    prefix: str
    region: str
    profile: str | None = None   # AWS config profile to read this source with
    role_arn: str | None = None  # role to assume, e.g. in a central log archive account

    @property
    def source(self):
        """The adapter for this type (an ingest.Source)."""
        return SOURCES[self.type]

    def session(self):
        """boto3 session for reading this source."""
        session = boto3.Session(profile_name=self.profile)
        if not self.role_arn:
            return session
        sts = session.client("sts", region_name=self.region)

        def assume():
            creds = sts.assume_role(RoleArn=self.role_arn, RoleSessionName="aiva")["Credentials"]
            return {"access_key": creds["AccessKeyId"], "secret_key": creds["SecretAccessKey"],
                    "token": creds["SessionToken"], "expiry_time": creds["Expiration"].isoformat()}

        # Refreshing credentials, because a large read can outlast one
        # assume-role session (1 hour by default).
        core = botocore.session.get_session()
        core._credentials = RefreshableCredentials.create_from_metadata(assume(), assume, "sts-assume-role")
        return boto3.Session(botocore_session=core)


@dataclass
class RunConfig:
    region: str  # where aiva calls Bedrock to classify
    from_date: datetime
    to_date: datetime | None = None  # defaults to now
    sources: list = field(default_factory=list)
    model: str = DEFAULT_MODEL
    output: str = "report"  # path without extension, local or s3://bucket/key
    format: str = "all"
    max_use_cases: int = 50
    show_samples: bool = False

    def window(self):
        """(from_date, to_date, days): the read window and its length for monthly projections."""
        to_date = self.to_date or datetime.now(timezone.utc)
        return self.from_date, to_date, max(1, round((to_date - self.from_date) / timedelta(days=1)))


_RUN_KEYS = {"region", "from", "to", "model", "output", "format", "max_use_cases",
             "show_samples", "sources"}
_SOURCE_KEYS = {"type", "bucket", "prefix", "region", "profile", "role_arn"}


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
    if "from" not in raw:
        raise ValueError(f"{path}: 'from' is required, a date like 2026-10-01")
    if not raw.get("sources"):
        raise ValueError(f"{path}: add at least one [[sources]] entry")

    settings = {k: v for k, v in raw.items() if k not in ("sources", "from", "to")}
    settings["from_date"] = _as_utc(raw["from"], whole_day=False, what=f"{path}: 'from'")
    if "to" in raw:
        settings["to_date"] = _as_utc(raw["to"], whole_day=True, what=f"{path}: 'to'")
        if settings["to_date"] <= settings["from_date"]:
            raise ValueError(f"{path}: 'to' must be after 'from'")

    sources = []
    for i, src in enumerate(raw["sources"], start=1):
        unknown = set(src) - _SOURCE_KEYS
        if unknown:
            raise ValueError(f"{path}: source {i}: unknown setting(s): {', '.join(sorted(unknown))}")
        if src.get("type") not in SOURCES:
            raise ValueError(f"{path}: source {i}: 'type' must be one of: {', '.join(SOURCES)}")
        if not src.get("bucket"):
            raise ValueError(f"{path}: source {i}: 'bucket' is required")
        sources.append(SourceConfig(**{"prefix": "", "region": raw["region"], **src}))

    return RunConfig(sources=sources, **settings)


def _as_utc(value, whole_day, what):
    """A TOML date or datetime as a UTC datetime. With whole_day, a date `to` includes that day."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        day = value + timedelta(days=1) if whole_day else value
        return datetime.combine(day, time(), tzinfo=timezone.utc)
    raise ValueError(f"{what} must be a date like 2026-10-01")
