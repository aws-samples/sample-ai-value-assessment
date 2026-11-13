"""Shared ingest layer: behaviour every source gets from aiva.ingest."""

import gzip
import json
import pathlib
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fake_s3 import FakeS3  # noqa: E402

from aiva.ingest import read_to_store  # noqa: E402
from aiva.invocation import Invocation  # noqa: E402
from aiva.sources import SOURCES  # noqa: E402
from aiva.store import AuditStore  # noqa: E402

DAY = "AWSLogs/111122223333/BedrockModelInvocationLogs/{region}/2026/11/12/03/"


def _record(i, **overrides):
    rec = {"timestamp": "2026-11-12T03:00:00Z", "requestId": str(i), "operation": "Converse",
           "modelId": "global.anthropic.claude-sonnet-4-6", "identity": {"arn": "arn:aws:sts::1:assumed-role/r/u"},
           "input": {"inputTokenCount": 10, "inputBodyJson": {"messages": []}},
           "output": {"outputTokenCount": 5, "outputBodyJson": {}}}
    rec.update(overrides)
    return rec


def _write(root, key, records=None, raw=None):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        raw = gzip.compress("\n".join(json.dumps(r) for r in records).encode())
    path.write_bytes(raw)


def _read(tmp_path, root, source="bedrock", prefix="", s3=None):
    store = AuditStore(str(tmp_path / "a.db"))
    with mock.patch("boto3.client", return_value=s3 or FakeS3(root)):
        result = read_to_store(store, SOURCES[source], "bucket", prefix, "ap-southeast-2", 7,
                               since="2026-01-01T00:00:00Z")
    rows = store.fetch_light_rows()
    store.close()
    return result, rows


def test_bad_object_and_bad_record_are_skipped_not_fatal(tmp_path):
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", raw=b'{"not": "gzip"}')
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T030100Z_b.json.gz",
           [_record(1), _record(2, input=None, output=None), _record(3, input={"inputTokenCount": None})])
    result, rows = _read(tmp_path, root)
    assert [r["request_id"] for r in rows] == ["1", "2", "3"]
    assert [r["input_tokens"] for r in rows] == [10, 0, 0]
    assert len(result.skipped) == 1 and result.skipped[0][1] == "BadGzipFile"


def test_access_denied_stops_the_run_with_a_hint(tmp_path):
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", [_record(1)])
    s3 = FakeS3(root)
    s3.get_object = mock.Mock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"))
    with pytest.raises(RuntimeError, match="kms:Decrypt"):
        _read(tmp_path, root, s3=s3)


def test_data_segment_in_prefix_does_not_skip_logs(tmp_path):
    root = tmp_path / "s3"
    _write(root, "platform/data/" + DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", [_record(1)])
    _, rows = _read(tmp_path, root, prefix="platform/data")
    assert len(rows) == 1


def test_objects_commit_oldest_first_across_regions(tmp_path):
    # Keys sort by region first; commits must follow LastModified so the
    # resume watermark cannot skip a region's earlier objects.
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T035000Z_a.json.gz", [_record("late")])
    _write(root, DAY.format(region="us-east-1") + "20261112T031000Z_b.json.gz", [_record("early")])
    result, rows = _read(tmp_path, root)
    assert [r["request_id"] for r in rows] == ["early", "late"]
    assert result.newest == "2026-11-12T03:50:00+00:00"


def test_otlp_reads_one_export_per_line(tmp_path):
    def export(model):
        attrs = [{"key": "event.name", "value": {"stringValue": "api_request"}},
                 {"key": "model", "value": {"stringValue": model}},
                 {"key": "input_tokens", "value": {"intValue": "100"}}]
        return {"resourceLogs": [{"resource": {"attributes": []},
                                  "scopeLogs": [{"logRecords": [{"timeUnixNano": "1762916400000000000",
                                                                 "attributes": attrs}]}]}]}
    root = tmp_path / "s3"
    (root / "otel").mkdir(parents=True)
    (root / "otel" / "20261112T030000Z.jsonl").write_text(
        json.dumps(export("claude-sonnet-4-6")) + "\n" + json.dumps(export("claude-opus-4-6")) + "\n")
    _, rows = _read(tmp_path, root, source="otlp", prefix="otel")
    assert sorted(r["model"] for r in rows) == ["claude-opus-4-6", "claude-sonnet-4-6"]


def test_invocation_coerces_types_once():
    inv = Invocation(input_tokens=None, output_tokens="7", max_tokens="0", messages=None,
                     metadata=None, timestamp=None, estimated_cost_usd=None)
    assert (inv.input_tokens, inv.output_tokens, inv.max_tokens) == (0, 7, None)
    assert (inv.messages, inv.metadata, inv.timestamp, inv.estimated_cost_usd) == ([], {}, "", 0.0)
