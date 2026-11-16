# AI Value Assessment (aiva)

Reads AI usage logs from S3 in the customer's account, groups them into sessions,
classifies them into use cases with Bedrock and reports STOP / REFINE / EXPAND.
Architecture and the reasons behind it: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Architecture rules

The code is layered. Each layer only knows the one below it.

1. **Transport** (`aiva/reader.py`, `aiva/otlp_reader.py`): decode a file format. No product knowledge.
2. **Dialects** (`aiva/dialects/`): one module per telemetry schema, `matches(event)` and `to_record(event)`. All product-specific rules (event names, attribute names, which events are calls) live here and nowhere else.
3. **Data model** (`aiva/invocation.py`): `Invocation` (one model call) and `Attachment` (context reported apart from a call).
4. **Store** (`aiva/store.py`): persists and returns records. No join or product logic.
5. **Attachments** (`aiva/attachments.py`): the only place that joins attachments onto invocations.
6. **Engines** (sessions in `aiva/cli.py`, `aiva/classifier.py`, reporters): only see invocations.

When extending:
- Keep the data model clean. Do not add product-specific fields to `Invocation` or subclass it. New context is a new `Attachment` kind, joined in `aiva/attachments.py`.
- A new OpenTelemetry product is a new dialect module plus one entry in `aiva/dialects/__init__.py`, ahead of `genai`.
- A new log source (not OTel) is a new adapter module with a `SOURCE` plus one entry in `aiva/sources.py`.
- If a change needs product knowledge in the store, sessions or classifier, it is in the wrong layer.
- Only model calls become invocations. Tool, hook and prompt events never count toward invocations or cost.

## Working here

- `uv` venv; run tests with `uv run pytest -q`.
- OTel tests use synthetic events shaped like the public monitoring docs. Never commit fixtures built from real OTel data. Bedrock golden fixtures are de-identified with `tests/fixtures/deidentify_bedrock.py`. Check real data locally only, and print counts, not content.
- AWS resources are never world-accessible (no `Principal: *`, no public buckets or Lambda URLs).
