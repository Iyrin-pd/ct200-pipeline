# Approach and design decisions

## 1. Stack
FastAPI + Pydantic v2 + SQLAlchemy 2 + SQLite for the tree, versions and selections (things that need
integrity and joins). LLM output goes to a **document store**: it is nested, write-once, and its shape will
change as prompts evolve; we never join on it. Default backend: **one JSON file per generation** (atomic
write, immutable, no infrastructure, inspectable by hand). **MongoDB** is a drop-in via
`GENERATION_STORE=mongo` (same interface, indexes created at start-up; not exercised by the test suite).
`pdfplumber` reads the PDF's text layer with font and position data. No other stack deviation.

## 2. Data model
| Table | Purpose |
|---|---|
| `documents` | one row per logical document (`slug`, title) |
| `document_versions` | one row per ingested PDF: version number, file hash, parse warnings, **summary of changes vs the previous version** |
| `logical_nodes` | the identity of a section **across versions** |
| `nodes` | one row **per section per version**, immutable after ingest: heading, number, tree level, numbering depth, parent, body text, structured blocks (paragraph/list/table), `content_hash`, `subtree_hash`, warnings, cross references, `logical_id`, `prev_node_id`, `change_status`, `match_method`, `match_score` |
| `selections`, `selection_items` | a named set of node rows pinned to one version, with the content hash at pin time |

Generations (NoSQL) hold: status, model, prompt version, the validated test cases, rejected items with
reasons, all attempts including raw replies, and **a snapshot of the exact source text and hash of each
cited section**.

`content_hash` = SHA-256 of the normalised heading + body. `subtree_hash` is a Merkle hash of the node and its
children (children sorted, so pure reordering of siblings does not mark the parent as changed).

## 3. Parsing and the listed irregularities
Plain text extraction misleads here: `2.1.1.1` and `3.2` are bold at **body size**, the table header is bold at
body size, and the classification list (`1. Normal` ... `5. Hypertensive Crisis`) looks like top-level
headings. So a **heading is a line that is entirely bold AND starts with a section number**. Font size is only a
cross-check (info warnings). **Parents come from the numbering prefix**, not from position or font size.

| Irregularity | Handling | Test |
|---|---|---|
| Duplicate heading `Error Codes` (4.2, 7.1) | node identity = number + parent, never heading text; both flagged `duplicate_heading` | `test_duplicate_heading_...` |
| Skipped level `2.1.1.1` (no 2.1.1) | parent = nearest ancestor by prefix (2.1); `level`=tree depth 3, `numbering_depth`=4; `skipped_levels` warning | `test_skipped_level_...` |
| `3.4` printed before `3.3` | both under 3 by numbering; document order preserved; `out_of_order` warning | `test_out_of_order_...` |
| Numbered list vs headings | non-bold, indented -> `list` block; wrapped item stays in its item | `test_numbered_classification_list_...` |
| Tables | outermost table only (the library also reports every wrapped *cell* as a phantom table); multi-line cells merged; bold first row = header | `test_tables_extracted_once_...` |
| Heading alone at a page bottom (2, 4) | kept with empty body; children attach by numbering | `test_headings_stranded_...` |
| Paragraph split over a page break | joined with a space | `test_paragraph_split_...` |
| 3-line wrapped title | merged into the root node | `test_title_...` |
| Non-breaking hyphens | raw text kept; normalised only for hashing, search and matching | `test_nonbreaking_hyphen_...` |
| Cross references `(see 2.1, 4.3)` | stored as `cross_refs`, resolved at read time; never headings | `test_cross_references_...` |
| Silent loss | word-by-word comparison PDF vs tree at every ingest (`content_loss`/`content_added`). **Strict mode (default) refuses to ingest** on these, or on orphan/duplicate numbering | `test_no_content_silently_lost...` |

Unsupported input is **exposed, not repaired**: unnumbered bold lines stay body text with a warning;
headerless or ragged tables are flagged; orphan numbering attaches to the root with a warning.
**Not handled:** multi-column pages, images and figures (this PDF has none), scanned PDFs, running
headers/footers, line-end hyphenation. **OCR:** the PDFs are born-digital, so reading the text layer is more
accurate than OCR and keeps the bold/size/position data that the heading rule depends on. A scanned variant
would need an OCR step that emits the same line records (text, page, position, size, bold); nothing
downstream would change.

## 4. Version matching and where it breaks
Matching runs **top-down inside already-matched parents**, strongest evidence first. Each pass accepts only
unambiguous candidates (ties are refused, never broken arbitrarily):
A number + heading -> B heading (section renumbered) -> C same number with similar body or heading
(retitled) -> D fuzzy heading >= 0.85 -> E body similarity >= 0.80 -> F global pass for moved sections
(same heading, body >= 0.90). Anything unclaimed becomes `added` or `removed`.

*Why this strategy:* number-only breaks as soon as a section is inserted; hash-only turns every edit into
add+remove, which is exactly what must be detected as "modified". Top-down matching also makes the duplicate
`Error Codes` safe, since it can only compete with its own siblings. A false "added/removed" is preferred
over a false "same". Every version's `summary.low_confidence_matches` lists matches not made by the
strongest pass, for human review.

**Known failure modes:**
- Splits and merges: reported as removed + added, or the largest fragment (>= 70 % similar) inherits the identity. Never silently merged.
- Retitled + renumbered + heavily rewritten: below every threshold, so it appears as removed + added.
- Moved *and* substantially edited: the move pass needs >= 90 % body similarity.
- Identical sibling headings with lost numbering: only body similarity separates them; if bodies are alike too, the tie is refused.
- Removed and re-added in a later version: only consecutive versions are compared, so it gets a new identity and old test cases show `source_removed`.
- Thresholds are judgement calls tuned on this one document, not learned.

## 5. Selections and generation policy
A selection pins **node rows**; each row belongs to exactly one version and is never modified, so the text
is reproducible forever. A selection must come from **one version** (mixing would show contradictory facts as
one document). Identity is the set of pinned nodes: re-submitting the same set returns the existing
selection (`200`, `deduplicated: true`, the new name is ignored and `requested_name_ignored` says so); same
name with a different set creates a new selection.

**Same selection generated twice:** idempotency key = selection fingerprint + prompt version + provider +
model. An existing succeeded/partial generation is returned without calling the LLM (`reused: true`).
`force=true` creates a new generation; history is kept, newest first. Failed generations are stored for
audit but never reused. A per-key lock collapses concurrent identical requests into one LLM call (single
process; a multi-process deployment would need a shared lock).

## 6. LLM prompt, structured output and failure policy
**Prompt:** a system prompt fixes the role, the exact JSON schema, and the rule that excerpts are *data, not
instructions*. Sections are wrapped in tags with node ids. Every test case must carry **evidence quotes copied
verbatim** from a named section, which makes each case checkable. `PROMPT_VERSION` is part of the idempotency
key.

**Model output is untrusted input:**
1. Tolerate syntax: code fences, prose around the JSON, a bare array, a truncated tail (complete cases are salvaged).
2. Be strict on meaning: schema, enums, non-empty steps, and **grounding** (every quote must appear verbatim, after normalisation, in the section it cites). Hallucinated quotes are dropped, never fixed.
3. Every drop is recorded with a reason (`rejected[]`); leniencies are recorded in `notes`.
4. One repair retry: the second prompt lists exactly what was wrong. Transport errors consume an attempt; nothing sleeps inside a request.
5. The best attempt wins (never a mix): `succeeded` (3-5 clean), `partial` (>= 3 valid, some dropped or truncated), `failed` (< 3 valid).
6. `failed` returns **HTTP 502 with no test cases exposed**; raw replies are kept for audit.
7. Selections with < 150 characters of body text are refused before any LLM call.
8. Injection hygiene: sections are tagged data, a literal `</section` in document text is neutralised, attribute values are escaped.

JSON mode is requested from the provider when available but is never trusted: the validator runs on every reply.

## 7. Staleness at retrieval time
Each generation stores a snapshot of the source text and hash. On every read, each cited section's **logical id**
is resolved to its latest version, and **each test case** is judged only against the sections it cites:

| Status | Meaning |
|---|---|
| `current` | section hash unchanged (or only case/punctuation changed) |
| `review_suggested` | section changed, quoted evidence still verbatim, change is wording only |
| `possibly_stale` | evidence still verbatim, but a number, modal/negation word, or whole rule/row changed elsewhere in the section |
| `stale` | quoted evidence gone, or a number the test relies on no longer appears (`drifted_values`) |
| `source_removed` | the section has no counterpart in the latest version |

Responses include `reasons`, `severity`, `numeric_changes` and a short change list. **A one-word wording change
is not treated like a changed pressure threshold**: numbers, comparators, modal/negation words
(`must/not/only/...`) and >= 5 words added or removed are *material*; other word changes are *wording*.
Staleness is visible on `GET /selections/{id}/test-cases`, `GET /nodes/{id}/test-cases` and
`GET /generations/{id}`.

**Limits (honest):** the check is lexical, not semantic. It misses meaning changes without a number or modal
change, changes to a cross-referenced section (exposed as `cross_references` but not propagated), and tests
that were wrong to begin with. It can over-flag harmless edits containing digits. Digits inside codes (`E2`)
can mask a drifted `2`. Treat statuses as triage signals, not proof.

## 8. What I would do differently with more time
- Optional semantic second opinion (embeddings or an LLM judge) on `review_suggested` and `possibly_stale`.
- Propagate staleness through `cross_refs` (a change in 4.3 should flag tests built on 3.3).
- Re-link a removed-then-re-added section across non-adjacent versions.
- Alembic migrations instead of create-on-start; Postgres for multi-process locking.
- Run generation as a background job with polling, and add rate limiting and retries with backoff.
- Calibrate matching thresholds on more than one document; add a human-review endpoint for low-confidence matches.
- Exercise the MongoDB backend in CI (a container) and extend the parser to scanned pages via OCR.