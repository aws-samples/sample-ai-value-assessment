import pathlib
import re
from unittest import mock

import pytest

from aiva.config import RunConfig, SourceConfig, load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
[[sources]]
type = "bedrock"
bucket = "a"
[[sources]]
type = "otlp"
bucket = "b"
prefix = "cowork/"
region = "us-east-1"
'''))
    assert [(s.type, s.prefix, s.region) for s in config.sources] == [
        ("bedrock", "", "ap-southeast-2"), ("otlp", "cowork/", "us-east-1")]


@pytest.mark.parametrize("text, message", [
    ('[[sources]]\ntype = "bedrock"\nbucket = "a"', "'region' is required"),
    ('region = "x"', "at least one [[sources]]"),
    ('region = "x"\nwindow = 7\n[[sources]]\ntype = "bedrock"\nbucket = "a"', "unknown setting(s): window"),
    ('region = "x"\n[[sources]]\ntype = "copilot"\nbucket = "a"', "'type' must be one of"),
    ('region = "x"\n[[sources]]\ntype = "bedrock"', "'bucket' is required"),
    ('region = "x"\nformat = "pdf"\n[[sources]]\ntype = "bedrock"\nbucket = "a"', "'format' must be one of"),
    ('region = ', "aiva.toml"),
])
def test_bad_config_gives_a_readable_error(tmp_path, text, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        load_config(_write(tmp_path, text))


def test_reports_upload_to_s3_output():
    from aiva.cli import _write_reports
    config = RunConfig(region="ap-southeast-2", output="s3://reports/aiva/run1", format="all",
                       sources=[SourceConfig("bedrock", "logs", "", "ap-southeast-2")])
    s3 = mock.Mock()
    with mock.patch("boto3.client", return_value=s3):
        _write_reports([], config)
    keys = sorted(call.args[1:] for call in s3.upload_file.call_args_list)
    assert keys == [("reports", "aiva/run1.html"), ("reports", "aiva/run1.json"), ("reports", "aiva/run1.md")]
