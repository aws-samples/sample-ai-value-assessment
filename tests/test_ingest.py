"""Shared ingest layer: behaviour every source gets from aiva.ingest."""

import gzip
import json
from datetime import datetime, timezone
import pathlib
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fake_s3 import FakeS3  # noqa: E402

from aiva.attachments import load_invocations  # noqa: E402
from aiva.config import SourceConfig  # noqa: E402
from aiva.ingest import read_to_store  # noqa: E402
from aiva.invocation import Invocation  # noqa: E402
from aiva.store import AuditStore  # noqa: E402

FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = "AWSLogs/111122223333/BedrockModelInvocationLogs/{region}/2026/11/12/03/"


def _src(source="bedrock", prefix=""):
    return SourceConfig(type=source, bucket="bucket", prefix=prefix, region="ap-southeast-2")


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
    result = read_to_store(store, _src(source, prefix), FROM, session=s3 or FakeS3(root))
    rows = store.fetch_light_rows()
    store.close()
    return result, rows


def test_cost_is_set_at_ingest_from_the_given_rates(tmp_path):
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", [_record(1)])  # 10 in, 5 out
    store = AuditStore(str(tmp_path / "a.db"))
    read_to_store(store, _src(), FROM, session=FakeS3(root), rates=(1_000_000, 2_000_000))
    assert store.fetch_light_rows()[0]["estimated_cost_usd"] == 10 * 1 + 5 * 2
    store.close()


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
    # Keys sort by region first; row ids still follow time.
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T035000Z_a.json.gz", [_record("late")])
    _write(root, DAY.format(region="us-east-1") + "20261112T031000Z_b.json.gz", [_record("early")])
    _, rows = _read(tmp_path, root)
    assert [r["request_id"] for r in rows] == ["early", "late"]


def _otlp_export(model):
    attrs = [{"key": "event.name", "value": {"stringValue": "api_request"}},
             {"key": "model", "value": {"stringValue": model}},
             {"key": "input_tokens", "value": {"intValue": "100"}}]
    return {"resourceLogs": [{"resource": {"attributes": []},
                              "scopeLogs": [{"logRecords": [{"timeUnixNano": "1762916400000000000",
                                                             "attributes": attrs}]}]}]}


def test_otlp_reads_one_export_per_line(tmp_path):
    root = tmp_path / "s3"
    (root / "otel").mkdir(parents=True)
    (root / "otel" / "20261112T030000Z.jsonl").write_text(
        json.dumps(_otlp_export("claude-sonnet-4-6")) + "\n" + json.dumps(_otlp_export("claude-opus-4-6")) + "\n")
    _, rows = _read(tmp_path, root, source="otlp", prefix="otel")
    assert sorted(r["model"] for r in rows) == ["claude-opus-4-6", "claude-sonnet-4-6"]


def _otlp_event(name, **attrs):
    """One Claude Code / Cowork log record, shaped as in the monitoring docs."""
    attrs = {"event.name": name, "session.id": "s-1", "user.id": "u-1", **attrs}
    return {"timeUnixNano": "1762916400000000000", "body": {"stringValue": f"claude_code.{name}"},
            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()]}


def _otlp_logs(*events, service="claude-code"):
    return {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                              "scopeLogs": [{"logRecords": list(events)}]}]}


def test_otlp_one_invocation_per_call_with_context_from_other_objects(tmp_path):
    # The collector batches by time, so a prompt and its calls can land in different objects.
    root = tmp_path / "s3"
    (root / "otel").mkdir(parents=True)
    (root / "otel" / "20261112T030000Z_a.json").write_text(json.dumps(_otlp_logs(
        _otlp_event("user_prompt", **{"prompt.id": "p-1", "prompt": "fix the failing test in billing"}),
        _otlp_event("hook_execution_start", **{"prompt.id": "p-1", "hook_name": "pre"}),
        _otlp_event("plugin_loaded", **{"plugin.name": "x"}))))
    (root / "otel" / "20261112T030100Z_b.json").write_text(json.dumps(_otlp_logs(
        _otlp_event("api_request", **{"prompt.id": "p-1", "model": "claude-sonnet-4-6", "request_id": "r-1",
                                      "input_tokens": "100", "output_tokens": "20", "cache_read_tokens": "5000"}),
        _otlp_event("tool_decision", **{"prompt.id": "p-1", "tool_name": "Bash", "decision": "accept"}),
        _otlp_event("tool_result", **{"prompt.id": "p-1", "tool_name": "Bash", "success": "true"}),
        _otlp_event("api_request", **{"prompt.id": "p-1", "model": "claude-sonnet-4-6", "request_id": "r-2",
                                      "input_tokens": "50", "output_tokens": "30"}),
        _otlp_event("assistant_response", **{"prompt.id": "p-1", "request_id": "r-2", "model": "claude-sonnet-4-6",
                                             "response": "fixed the rounding in the billing test"}))))
    store = AuditStore(str(tmp_path / "a.db"))
    read_to_store(store, _src("otlp", "otel"), FROM, session=FakeS3(root))
    rows = store.fetch_light_rows()
    assert [(r["request_id"], r["model"], r["session_key"]) for r in rows] == [
        ("r-1", "claude-sonnet-4-6", "s-1"), ("r-2", "claude-sonnet-4-6", "s-1")]
    first, second = load_invocations(store, [r["id"] for r in rows])
    assert first["messages"] == [{"role": "user", "content": "fix the failing test in billing"}]
    assert first["tools"] == ["Bash"]
    assert (first["response_text"], second["response_text"]) == ("", "fixed the rounding in the billing test")
    assert second["messages"] == [] and second["tools"] == []
    # Context objects count as read, so a resume does not fetch them again.
    assert {"otel/20261112T030000Z_a.json", "otel/20261112T030100Z_b.json"} <= store.stored_keys()
    store.close()


def test_otlp_cowork_call_without_prompt_context_still_counts(tmp_path):
    root = tmp_path / "s3"
    (root / "otel").mkdir(parents=True)
    (root / "otel" / "20261112T030000Z_a.json").write_text(json.dumps(_otlp_logs(
        _otlp_event("api_request", **{"prompt.id": "p-9", "model": "claude-opus-4-6", "input_tokens": "2",
                                      "cache_creation_tokens": "36000"}),
        _otlp_event("user_prompt", prompt_length="3"),  # prompt logging off, no text and no prompt.id
        service="cowork")))
    _, rows = _read(tmp_path, root, source="otlp", prefix="otel")
    assert [(r["model"], r["cache_write_tokens"]) for r in rows] == [("claude-opus-4-6", 36000)]


def test_invocation_coerces_types_once():
    inv = Invocation(input_tokens=None, output_tokens="7", max_tokens="0", messages=None,
                     metadata=None, timestamp=None, estimated_cost_usd=None)
    assert (inv.input_tokens, inv.output_tokens, inv.max_tokens) == (0, 7, None)
    assert (inv.messages, inv.metadata, inv.timestamp, inv.estimated_cost_usd) == ([], {}, "", 0.0)


def test_two_sources_read_into_one_store(tmp_path):
    bedrock_root, otel_root = tmp_path / "bedrock", tmp_path / "otel"
    _write(bedrock_root, DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", [_record("b1"), _record("b2")])
    (otel_root / "otel").mkdir(parents=True)
    (otel_root / "otel" / "20261112T030000Z.json").write_text(json.dumps(_otlp_export("claude-sonnet-4-6")))
    store = AuditStore(str(tmp_path / "a.db"))
    for source, prefix, root in (("bedrock", "", bedrock_root), ("otlp", "otel", otel_root)):
        read_to_store(store, _src(source, prefix), FROM, session=FakeS3(root))
    rows = store.fetch_light_rows()
    store.close()
    assert sorted((r["operation"], r["model"]) for r in rows) == [
        ("Converse", "global.anthropic.claude-sonnet-4-6"), ("Converse", "global.anthropic.claude-sonnet-4-6"),
        ("api_request", "claude-sonnet-4-6")]


def test_to_date_drops_objects_written_after_it(tmp_path):
    root = tmp_path / "s3"
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz", [_record("in")])
    _write(root, DAY.format(region="ap-southeast-2") + "20261112T050000Z_b.json.gz", [_record("out")])
    store = AuditStore(str(tmp_path / "a.db"))
    read_to_store(store, _src(), FROM, to_date=datetime(2026, 11, 12, 4, tzinfo=timezone.utc), session=FakeS3(root))
    assert [r["request_id"] for r in store.fetch_light_rows()] == ["in"]
    store.close()


def test_rerun_into_same_store_fetches_only_missing_objects(tmp_path):
    # A run killed partway through a central bucket (two regions) resumes
    # without re-downloading what it already stored, and without gaps.
    root = tmp_path / "s3"
    keys = [DAY.format(region="ap-southeast-2") + "20261112T030000Z_a.json.gz",
            DAY.format(region="us-east-1") + "20261112T020000Z_b.json.gz",
            DAY.format(region="us-east-1") + "20261112T040000Z_c.json.gz"]
    for n, key in enumerate(keys):
        _write(root, key, [_record(f"{n}-0"), _record(f"{n}-1")])
    s3 = FakeS3(root)
    fetched = []
    get_object = s3.get_object
    s3.get_object = lambda Bucket, Key: fetched.append(Key) or get_object(Bucket, Key)

    store = AuditStore(str(tmp_path / "a.db"))
    last = s3.objects.pop(keys[2])  # first run: the last object has not arrived yet
    read_to_store(store, _src(), FROM, session=s3)
    s3.objects[keys[2]] = last
    fetched.clear()
    read_to_store(store, _src(), FROM, session=s3)
    rows = store.fetch_light_rows()
    store.close()
    assert fetched == [keys[2]]
    assert sorted(r["request_id"] for r in rows) == ["0-0", "0-1", "1-0", "1-1", "2-0", "2-1"]
