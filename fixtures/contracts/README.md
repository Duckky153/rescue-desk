# Synthetic contract fixtures

Every file in this directory is invented test data. None is a customer contract,
legal template, or statement of any real vendor's terms.

`ground_truth.json` is the machine-readable oracle. It records the SHA-256 and
expected extraction behavior for:

- a clean contract;
- a base agreement with conflicting amendment terms;
- redacted dates and fees;
- instruction-like prompt-injection text;
- multiple date, currency, fee, and cadence formats;
- a page with no extractable text that must be routed to OCR; and
- encrypted, attachment-bearing, and invalid-header safety rejections.

Regenerate the complete set from the repository root:

```sh
uv run --project backend python scripts/generate_contract_fixtures.py
```

Generation is deterministic. Repeated runs produce the same PDF bytes, hashes,
and ground truth. When a fixture changes intentionally, regenerate the complete
set and run `uv run --project backend pytest backend/tests/extraction`.
