import pathlib
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from aiva.config import RunConfig, SourceConfig, load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = '\n[[sources]]\ntype = "bedrock"\nbucket = "a"'


def _write(tmp_path, text):
    path = tmp_path / "aiva.toml"
    path.write_text(text)
    return path


def test_example_config_loads():
    config = load_config(ROOT / "aiva.example.toml")
    assert config.region == "ap-southeast-2"
    assert config.sources == [SourceConfig(type="bedrock", bucket="my-bedrock-invocation-logs",
                                           prefix="", region="ap-southeast-2")]


def test_sources_default_region_and_prefix(tmp_path):
    config = load_config(_write(tmp_path, '''
region = "ap-southeast-2"
from = 2026-10-01
[[sources]]
type = "bedrock"
bucket = "a"
[[sources]]
type = "otlp"
bucket = "b"
prefix = "cowork/"
region = "us-east-1"
profile = "log-archive"
'''))
    assert [(s.type, s.prefix, s.region, s.profile) for s in config.sources] == [
        ("bedrock", "", "ap-southeast-2", None), ("otlp", "cowork/", "us-east-1", "log-archive")]


@pytest.mark.parametrize("text, message", [
    ('from = 2026-10-01' + SOURCE, "'region' is required"),
    ('region = "x"' + SOURCE, "'from' is required"),
    ('region = "x"\nfrom = 2026-10-01', "at least one [[sources]]"),
    ('region = "x"\nfrom = 2026-10-01\ndays = 7' + SOURCE, "unknown setting(s): days"),
    ('region = "x"\nfrom = 2026-10-01\n[[sources]]\ntype = "copilot"\nbucket = "a"', "'type' must be one of"),
    ('region = "x"\nfrom = 2026-10-01\n[[sources]]\ntype = "bedrock"', "'bucket' is required"),
    ('region = "x"\nfrom = 2026-10-01\nformat = "pdf"' + SOURCE, "'format' must be one of"),
    ('region = "x"\nfrom = 2026-10-31\nto = 2026-10-01' + SOURCE, "'to' must be after 'from'"),
    ('region = "x"\nfrom = "October"' + SOURCE, "must be a date"),
    ('region = ', "aiva.toml"),
    ('region = "x"\nfrom = 2026-10-01\ninput_per_1m = 0' + SOURCE, "'input_per_1m' must be a positive number"),
    ('region = "x"\nfrom = 2026-10-01\noutput_per_1m = "15"' + SOURCE, "'output_per_1m' must be a positive number"),
])
def test_bad_config_gives_a_readable_error(tmp_path, text, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        load_config(_write(tmp_path, text))


def test_cost_rates_default_and_override(tmp_path):
    assert load_config(_write(tmp_path, 'region = "x"\nfrom = 2026-10-01' + SOURCE)).rates == (3.0, 15.0)
    config = load_config(_write(tmp_path, 'region = "x"\nfrom = 2026-10-01\ninput_per_1m = 2\noutput_per_1m = 10.5' + SOURCE))
    assert config.rates == (2, 10.5)


def test_to_date_includes_the_whole_day(tmp_path):
    config = load_config(_write(tmp_path, 'region = "x"\nfrom = 2026-10-01\nto = 2026-10-31' + SOURCE))
    from_date, to_date, days = config.window()
    assert (from_date.isoformat(), to_date.isoformat(), days) == (
        "2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00", 31)


def test_role_arn_reads_with_assumed_credentials():
    sts = mock.Mock()
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "AKIAASSUMED", "SecretAccessKey": "s", "SessionToken": "t",
        "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)}}
    src = SourceConfig("bedrock", "logs", "", "ap-southeast-2", role_arn="arn:aws:iam::1:role/r")
    with mock.patch("boto3.Session.client", return_value=sts):
        session = src.session()
    assert session.get_credentials().access_key == "AKIAASSUMED"
    sts.assume_role.assert_called_once_with(RoleArn="arn:aws:iam::1:role/r", RoleSessionName="aiva")


def test_reports_upload_to_s3_output():
    from aiva.cli import _write_reports
    config = RunConfig(region="ap-southeast-2", from_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
                       output="s3://reports/aiva/run1", format="all",
                       sources=[SourceConfig("bedrock", "logs", "", "ap-southeast-2")])
    s3 = mock.Mock()
    with mock.patch("boto3.client", return_value=s3):
        _write_reports([], config)
    keys = sorted(call.args[1:] for call in s3.upload_file.call_args_list)
    assert keys == [("reports", "aiva/run1.html"), ("reports", "aiva/run1.json"), ("reports", "aiva/run1.md")]


def test_every_setting_is_documented():
    from aiva.config import _RUN_KEYS, _SOURCE_KEYS
    doc = (ROOT / "docs" / "config.md").read_text()
    missing = [k for k in sorted((_RUN_KEYS - {"sources"}) | _SOURCE_KEYS) if f"| `{k}` |" not in doc]
    assert not missing, f"add to docs/config.md: {missing}"


def test_config_reads_from_s3():
    from botocore.exceptions import ClientError
    s3 = mock.Mock()
    s3.get_object.return_value = {"Body": mock.Mock(read=lambda: ('region = "x"\nfrom = 2026-10-01' + SOURCE).encode())}
    with mock.patch("boto3.client", return_value=s3):
        assert load_config("s3://configs/aiva/aiva.toml").region == "x"
    s3.get_object.assert_called_once_with(Bucket="configs", Key="aiva/aiva.toml")

    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    with mock.patch("boto3.client", return_value=s3), pytest.raises(ValueError, match="aiva.toml: no such object"):
        load_config("s3://configs/aiva/aiva.toml")


@pytest.mark.parametrize("path, message", [("s3://configs", "expected s3://bucket/key"),
                                           ("missing.toml", "missing.toml: No such file or directory")])
def test_bad_config_path_gives_a_readable_error(path, message):
    with pytest.raises(ValueError, match=message):
        load_config(path)
