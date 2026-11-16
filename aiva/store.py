"""SQLite-backed store for invocation records.

Model Invocation Logs at scale (hundreds of thousands to millions of
invocations across a large org) cannot fit as a single in-memory Python
list, each invocation's raw system_prompt/messages/response_text is tens of
KB, and Python object overhead multiplies that by 2-4x. A week of a
300-engineer org's daily usage was measured at 12-122GB resident if held as
one list (see docs/ARCHITECTURE.md's memory-ceiling section). This store
keeps invocations on local disk instead: session-grouping columns (caller,
timestamp, cost, session key) are real SQL columns queryable without
touching the heavy content; the heavy content (system_prompt, messages,
response_text, tools, metadata) is a JSON blob fetched only for the specific
invocations currently being classified.

The store is a local SQLite file, not a network service, nothing leaves the
account, only the write path (during S3 fetch) and read path (during Pass 1)
change shape.

Persisting to disk also makes the S3 fetch resumable, not just memory-safe:
a real 30GB / ~1M-invocation window took an estimated 5-8 hours end to end,
long enough that a killed process (network blip, laptop sleep, an operator
who needs to stop and restart later) previously meant starting the S3 fetch
over from zero. Each row is keyed on its S3 object key (UNIQUE, INSERT OR
IGNORE), and `stored_keys()` tells the reader which objects are already
committed, so a re-run into the same --db fetches only what is missing.
"""

import json
import sqlite3
import threading

from aiva.invocation import PromptContext

# requestMetadata keys that carry an explicit session/conversation identifier.
# Checked in order; first present wins. Moved here (from cli.py) because
# session_key is now computed once, at insert time, not re-derived from raw
# metadata on every grouping pass.
_SESSION_ID_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId",
                     "conversationID", "session", "thread_id", "threadId")

_LIGHT_COLUMNS = (
    "id", "timestamp", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "estimated_cost_usd",
    "caller", "operation", "request_id", "max_tokens",
    "session_key", "is_audit_overhead",
)
# Must list the exact same columns, in the exact same order, as the SELECT in
# fetch_light_rows below (checked there against cur.description at runtime).
# Kept as a hardcoded literal in that query, not built by joining this tuple
# at query time, so the query passed to execute() is fixed source text.



class AuditStore:
    """One SQLite file per audit run. Not safe to share a connection across
    threads; call get_connection() from each thread and it will lazily open
    (and cache) a thread-local connection to the same file."""

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        conn = self._connect()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                s3_key TEXT UNIQUE,
                last_modified TEXT,
                timestamp TEXT,
                model TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                caller TEXT,
                operation TEXT,
                request_id TEXT,
                max_tokens INTEGER,
                session_key TEXT,
                is_audit_overhead INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL
            )
        """)
        # PromptContext rows: prompt text, response text and tool names from
        # sources that report them as separate events (OTel). Never counted
        # or priced, only attached to calls in fetch_full_invocations.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                s3_key TEXT UNIQUE,
                prompt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                request_id TEXT,
                timestamp TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_context_prompt ON context(prompt_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_key ON invocations(session_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_overhead ON invocations(is_audit_overhead)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_last_modified ON invocations(last_modified)")
        conn.commit()

    def _connect(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path)
        return self._local.conn

    def insert_invocations(self, items):
        """Insert a batch of (record, s3_key, last_modified_iso) tuples in one
        transaction. `record` is an aiva.invocation.Invocation or
        PromptContext. Call from
        a single thread only, the S3 fetch's worker threads run concurrently
        but the caller collects their results and inserts on the main thread,
        avoiding concurrent SQLite writers entirely.

        INSERT OR IGNORE on s3_key (UNIQUE): re-inserting a row already
        present is a silent no-op, not a duplicate row and not an error.
        """
        conn = self._connect()
        context = [(s3_key, c.prompt_id, c.kind, c.text, c.request_id, c.timestamp)
                   for c, s3_key, _ in items if isinstance(c, PromptContext)]
        rows = [_to_row(record, s3_key, last_modified) for record, s3_key, last_modified in items
                if not isinstance(record, PromptContext)]
        conn.executemany(
            "INSERT OR IGNORE INTO context (s3_key, prompt_id, kind, text, request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            context,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO invocations (s3_key, last_modified, timestamp, model, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "estimated_cost_usd, caller, operation, request_id, max_tokens, "
            "session_key, is_audit_overhead, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    def stored_keys(self):
        """S3 keys of objects already committed. Each object's first record
        keeps the bare key (later ones get "#i"), and an object's records are
        always committed together, so a bare key means the whole object is in."""
        conn = self._connect()
        return {row[0] for row in conn.execute(
            "SELECT s3_key FROM invocations WHERE s3_key NOT LIKE '%#%' "
            "UNION SELECT s3_key FROM context WHERE s3_key NOT LIKE '%#%'")}

    def fetch_light_rows(self):
        """Return all non-audit-overhead rows with only the light (session
        grouping) columns, ordered by insertion order. Never touches payload,
        so this stays cheap even at a million invocations."""
        conn = self._connect()
        cur = conn.execute(
            "SELECT id, timestamp, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, estimated_cost_usd, "
            "caller, operation, request_id, max_tokens, "
            "session_key, is_audit_overhead FROM invocations "
            "WHERE is_audit_overhead = 0 ORDER BY id ASC"
        )
        actual_columns = tuple(d[0] for d in cur.description)
        if actual_columns != _LIGHT_COLUMNS:
            raise RuntimeError(
                f"query column order drifted from _LIGHT_COLUMNS: {actual_columns}")
        return [dict(zip(_LIGHT_COLUMNS, row)) for row in cur.fetchall()]

    def fetch_full_invocations(self, ids):
        """Return full invocation dicts (the Invocation fields plus the row id)
        for the given row ids, ordered to match insertion order.
        Called once per session, from a Pass-1 worker thread, so at most
        PASS1_MAX_WORKERS sessions' full payloads are resident at once.

        The id list is variable-length (one session can span any number of
        invocations), so it can't be a fixed count of `?` placeholders. It is
        passed as a single bound JSON array parameter and unpacked inside
        SQLite via json_each, rather than building a "(?,?,?,...)" clause
        sized to len(ids): the query text is then a fixed literal, and ids
        never enter the SQL string itself, only as bound parameter values.
        """
        if not ids:
            return []
        conn = self._connect()
        cur = conn.execute(
            "SELECT id, timestamp, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, estimated_cost_usd, caller, operation, "
            "request_id, max_tokens, payload FROM invocations "
            "WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id ASC",
            (json.dumps(list(ids)),),
        )
        invocations = [_from_row(row) for row in cur.fetchall()]
        _attach_context(conn, invocations)
        return invocations

    def overhead_summary(self):
        """Return (count, total_cost_usd) for AI Value Assessment's own audit-overhead calls."""
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd), 0) FROM invocations "
            "WHERE is_audit_overhead = 1"
        ).fetchone()
        return row[0], row[1]

    def close(self):
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn


def _session_key(record):
    """Explicit session/conversation id from requestMetadata, or None.

    None means this invocation falls back to caller + time-gap grouping,
    same fallback rule as before, just evaluated once at write time instead
    of on every grouping pass.
    """
    for key in _SESSION_ID_KEYS:
        val = record.metadata.get(key)
        if val:
            return str(val)
    return None


def _is_audit_overhead(record):
    return record.metadata.get("source") == "aiva-audit"


def _to_row(record, s3_key, last_modified):
    payload = json.dumps({
        "system_prompt": record.system_prompt,
        "tools": record.tools,
        "messages": record.messages,
        "response_text": record.response_text,
        "user_agent": record.user_agent,
        "metadata": record.metadata,
    })
    return (
        s3_key,
        last_modified,
        record.timestamp,
        record.model,
        record.input_tokens,
        record.output_tokens,
        record.cache_read_tokens,
        record.cache_write_tokens,
        record.estimated_cost_usd,
        record.caller,
        record.operation,
        record.request_id,
        record.max_tokens,
        _session_key(record),
        1 if _is_audit_overhead(record) else 0,
        payload,
    )


def _from_row(row):
    (rid, timestamp, model, input_tokens, output_tokens, cache_read_tokens,
     cache_write_tokens, estimated_cost_usd, caller, operation,
     request_id, max_tokens, payload_json) = row
    payload = json.loads(payload_json)
    return {
        "id": rid,
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "caller": caller,
        "operation": operation,
        "request_id": request_id,
        "max_tokens": max_tokens,
        "system_prompt": payload.get("system_prompt", ""),
        "tools": payload.get("tools", []),
        "messages": payload.get("messages", []),
        "response_text": payload.get("response_text", ""),
        "user_agent": payload.get("user_agent", ""),
        "metadata": payload.get("metadata", {}),
    }


def _attach_context(conn, invocations):
    """Put each prompt's text and tool names on its first call, and each
    response on the call with its request_id (else the first call)."""
    first_call = {}
    for inv in invocations:
        prompt_id = inv["metadata"].get("prompt_id")
        if prompt_id and prompt_id not in first_call:
            first_call[prompt_id] = inv
    if not first_call:
        return
    by_request = {inv["request_id"]: inv for inv in invocations if inv["request_id"]}
    cur = conn.execute(
        "SELECT prompt_id, kind, text, request_id FROM context "
        "WHERE prompt_id IN (SELECT value FROM json_each(?)) ORDER BY timestamp, id",
        (json.dumps(list(first_call)),),
    )
    for prompt_id, kind, text, request_id in cur.fetchall():
        call = first_call[prompt_id]
        if kind == "prompt" and not call["messages"]:
            call["messages"] = [{"role": "user", "content": text}]
        elif kind == "tool" and text not in call["tools"]:
            call["tools"].append(text)
        elif kind == "response":
            call = by_request.get(request_id, call)
            if not call["response_text"]:
                call["response_text"] = text
