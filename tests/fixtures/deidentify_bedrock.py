"""Copy real Model Invocation Log objects into tests/fixtures/bedrock, de-identified.

Structure is kept (record batching, block types, token counts, model ids,
operations); account ids, identities, S3 URIs and all prompt/response text
are replaced deterministically.

    python tests/fixtures/deidentify_bedrock.py <dir of raw AWSLogs objects> <account id>
"""

import gzip
import hashlib
import json
import pathlib
import sys

from aiva.reader import _iter_json_documents

OUT = pathlib.Path(__file__).parent / "bedrock"
FAKE_ACCOUNT = "111122223333"
TEXT_KEYS = {"text", "signature"}


def _tag(value):
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _scrub(node, account, key=None):
    if isinstance(node, dict):
        return {k: _scrub(v, account, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(v, account, key) for v in node]
    if not isinstance(node, str):
        return node
    if key in TEXT_KEYS:
        return f"redacted-{key}-{_tag(node)}"
    if key == "arn" and ":sts::" in node:
        return f"arn:aws:sts::{FAKE_ACCOUNT}:assumed-role/role-{_tag(node)}/user-{_tag(node[::-1])}"
    if key == "s3Uri":
        return f"s3://example-log-bucket/data/{_tag(node)}.png"
    if key in ("guardrailId", "guardrailArn"):
        node = node.rsplit("/", 1)[0] + "/" + _tag(node) if "/" in node else _tag(node)
    if key == "source" and node != "aiva-audit":
        return f"redacted-{_tag(node)}"
    return node.replace(account, FAKE_ACCOUNT)


def _features(docs):
    found = set()
    for d in docs:
        model = d.get("modelId", "")
        found.add("app-profile" if "application-inference-profile" in model else
                  "profile-arn" if model.startswith("arn:") else model.split(".")[1])
        if d["input"].get("cacheReadInputTokenCount"):
            found.add("cache")
        if d.get("requestMetadata"):
            found.add("metadata")
        if "trace" in d["output"].get("outputBodyJson", {}):
            found.add("trace")
        if "system" not in d["input"].get("inputBodyJson", {}):
            found.add("no-system")
        blob = json.dumps(d)
        found.update(name for name in ("guardContent", "reasoningContent") if name in blob)
    if len(docs) > 1:
        found.add("multi-record")
    return found


def main(raw_dir, account):
    objects = []
    for path in sorted(pathlib.Path(raw_dir).rglob("*.json.gz")):
        docs = list(_iter_json_documents(gzip.decompress(path.read_bytes()).decode("utf-8")))
        objects.append((path, docs))
    # Smallest objects first, keep one whenever it adds a feature not yet covered.
    covered = set()
    for path, docs in sorted(objects, key=lambda o: (len(o[1]), o[0].stat().st_size)):
        new = _features(docs) - covered
        if not new or len(docs) > 10:
            continue
        covered |= new
        rel = str(path.relative_to(raw_dir)).replace(account, FAKE_ACCOUNT)
        out = OUT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(_scrub(d, account)) for d in docs) + "\n"
        out.write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
        print(f"{len(docs):>3} records  {sorted(new)}  {rel}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
