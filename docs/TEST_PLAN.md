# Test plan

Run everything: `pytest -q` (82 tests, offline). Live flow: `python scripts\demo.py`.

| Area | File | What it proves |
|---|---|---|
| Parser (21) | tests/test_parser.py | each irregularity: duplicate heading, skipped level, out-of-order, list vs heading, tables, page-bottom headings, page-split paragraphs, title, non-breaking hyphen, cross refs, no lost text, tree integrity, exact v1->v2 hash changes |
| Matching (8) | tests/test_matching.py | real v1->v2, duplicates never cross-matched, renumbering, retitle, moved section, split not merged, ambiguous twins |
| Diff (7) | tests/test_diffing.py | none / cosmetic / wording / material, negation flip, one changed threshold |
| LLM failures (18) | tests/test_llm_validation.py | fenced/wrapped JSON, truncation, wrong shape + repair, prose-only reply, hallucinated quote, foreign node id, missing fields, duplicates, >5 cases, <3 valid = failed, transport errors, injection |
| Staleness (10) | tests/test_staleness.py | each status, numeric drift, multi-node worst-wins, no input mutation |
| Store (2) | tests/test_store.py | filters, newest first, immutability, no path traversal |
| Database (5) | tests/test_ingest_db.py | ingest v1/v2, logical identity, v1 untouched, no-op re-ingest, changes + search |
| API (11) | tests/test_api.py | whole flow over HTTP incl. pinned selections, idempotency, 502 on bad LLM output, staleness after re-ingest |

## Extra edge cases to try by hand in Swagger
- Upload a non-PDF to `/documents/ct200/versions/upload` -> 422.
- Select a heading-only section (e.g. section 4) and generate -> 422 asking for descendants.
- Select nodes from v1 and v2 together -> 422.
- Select the title root -> 422.
- Same selection twice with different names -> 200, `deduplicated: true`.
- Generate with `LLM_PROVIDER=openai_compat` and no key -> 502 with an audit trail.