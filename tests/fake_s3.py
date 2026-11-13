"""In-memory S3 client serving a local directory, for reading log fixtures end to end."""

import io
import pathlib
import re
from datetime import datetime, timezone

_KEY_TIME = re.compile(r"(\d{8}T\d{6})")


class FakeS3:
    """Lists and gets objects under `root`. LastModified comes from the
    timestamp in the key (Bedrock names objects by delivery time), so runs
    are deterministic."""

    def __init__(self, root):
        root = pathlib.Path(root)
        self.objects = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                key = path.relative_to(root).as_posix()
                match = _KEY_TIME.search(path.name)
                lm = (datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                      if match else datetime(2026, 1, 1, tzinfo=timezone.utc))
                self.objects[key] = (path.read_bytes(), lm)

    def get_paginator(self, _name):
        return self

    def paginate(self, Bucket, Prefix="", Delimiter=None):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        if Delimiter:
            subdirs = sorted({Prefix + k[len(Prefix):].split(Delimiter, 1)[0] + Delimiter
                              for k in keys if Delimiter in k[len(Prefix):]})
            yield {"CommonPrefixes": [{"Prefix": p} for p in subdirs]}
            return
        yield {"Contents": [{"Key": k, "LastModified": self.objects[k][1]} for k in keys]}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}
