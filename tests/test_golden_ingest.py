"""Golden test: de-identified real Bedrock log objects read end to end through
the S3 reader into the store. Store rows and session grouping must match
tests/fixtures/bedrock_golden.json, which was recorded from main before the
ingest refactor.

Regenerate (only when a change in output is intended):
    python tests/test_golden_ingest.py tests/fixtures/bedrock > tests/fixtures/bedrock_golden.json
"""

import json
import pathlib
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fake_s3 import FakeS3  # noqa: E402

from aiva.cli import _group_into_sessions  # noqa: E402
from aiva.store import AuditStore  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _read(store, s3):
    from aiva.reader import read_invocation_logs_to_store
    with mock.patch("boto3.client", return_value=s3):
        return read_invocation_logs_to_store(store, "bucket", "", "ap-southeast-2", 7, since="2026-01-01T00:00:00Z")


def snapshot(root, db_path):
    store = AuditStore(str(db_path))
    try:
        newest = _read(store, FakeS3(root))
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM invocations ORDER BY s3_key")]
        key_by_id = {r.pop("id"): r["s3_key"] for r in rows}
        # Row ids follow fetch order, so sessions are compared by S3 key, not id.
        sessions = sorted(
            ({"keys": sorted(key_by_id[i] for i in s["invocation_ids"]), "caller": s["caller"],
              "cost": round(s["total_cost_usd"], 9), "first_ts": str(s["first_ts"]), "last_ts": str(s["last_ts"])}
             for s in _group_into_sessions(store.fetch_light_rows())),
            key=lambda s: s["keys"])
        return {"newest": newest, "rows": rows, "sessions": sessions}
    finally:
        store.close()


def test_bedrock_fixtures_match_golden(tmp_path):
    expected = json.loads((FIXTURES / "bedrock_golden.json").read_text())
    assert snapshot(FIXTURES / "bedrock", tmp_path / "a.db") == expected


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        print(json.dumps(snapshot(sys.argv[1], pathlib.Path(d) / "a.db"), indent=1, sort_keys=True))
