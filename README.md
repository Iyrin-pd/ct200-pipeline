# CT-200 Document Intelligence API

Parses the CardioTrack CT-200 manual PDF into a **versioned tree**, lets you **select sections**
(pinned to a version), generates **QA test-case ideas** with an LLM, and tells you **at retrieval time
whether each test case still matches the current document**.

Design decisions, irregularity handling, matching strategy and limits: [`docs/APPROACH.md`](docs/APPROACH.md).
Test cases and edge cases: [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md).

## Setup (Windows PowerShell)

```powershell
git clone https://github.com/<your-username>/ct200-pipeline.git
cd ct200-pipeline
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS/Linux: use `source .venv/bin/activate`. Python 3.10+ is required.

The two source PDFs are in `data/` (`ct200_manual.pdf` = v1, `ct200_manual_v2.pdf` = v2).

## Run the tests

```powershell
pytest -q
```

82 tests, no network or API key needed (the LLM is mocked/scripted).

## Run the API

```powershell
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive Swagger UI.
SQLite lives in `var/ct200.db`; generated test cases in `var/generations/*.json` (both git-ignored).
To reset everything: stop the server and delete the `var` folder.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (offline, deterministic) or `openai_compat` (Groq, Gemini, OpenRouter, ...) |
| `LLM_BASE_URL` | - | e.g. `https://api.groq.com/openai/v1` |
| `LLM_API_KEY` | - | provider key (never commit it) |
| `LLM_MODEL` | - | model name from your provider's dashboard |
| `LLM_JSON_MODE` | `true` | ask the provider for JSON output; validation runs regardless |
| `GENERATION_STORE` | `json` | `json` (files) or `mongo` |
| `GENERATION_DIR` | `var/generations` | where JSON generations are written |
| `MONGO_URI` | - | needed only if `GENERATION_STORE=mongo` |
| `DATABASE_URL` | `sqlite:///var/ct200.db` | SQLAlchemy URL |
| `DATA_DIR` | `data/` | folder PDFs can be ingested from |
| `MIN_SOURCE_CHARS` | `150` | refuse to call the LLM below this much selected text |

Real LLM example (PowerShell, before starting uvicorn):

```powershell
$env:LLM_PROVIDER="openai_compat"
$env:LLM_BASE_URL="https://api.groq.com/openai/v1"
$env:LLM_API_KEY="<your key>"
$env:LLM_MODEL="<model name>"
```

## Trigger the v1 -> v2 re-ingestion flow

**Automated (recommended):** with the server running on an empty database,

```powershell
python scripts\demo.py
```

It ingests v1, creates two selections, generates test cases, ingests v2, shows what changed, and
prints each test case's staleness.

**Manually (Swagger UI or curl):**

```powershell
# 1. ingest v1, then v2 (same document slug = same document, new version)
curl -X POST http://127.0.0.1:8000/documents/ct200/versions -H "Content-Type: application/json" -d "{\"path\":\"ct200_manual.pdf\"}"
curl -X POST http://127.0.0.1:8000/documents/ct200/versions -H "Content-Type: application/json" -d "{\"path\":\"ct200_manual_v2.pdf\"}"
# 2. see what changed between versions
curl http://127.0.0.1:8000/documents/ct200/versions/2
# 3. per-node change + diff (use a v1 node id from /documents/ct200/tree?version=1)
curl http://127.0.0.1:8000/nodes/<node_id>/changes
```

Order that shows staleness: ingest v1 -> `POST /selections` -> `POST /selections/{id}/generate`
-> ingest v2 -> `GET /selections/{id}/test-cases`.

## Endpoints

| Area | Endpoint |
|---|---|
| Ingest | `POST /documents/{slug}/versions` (path in data dir) - `POST /documents/{slug}/versions/upload` |
| Browse | `GET /documents/{slug}/sections?version=` - `/tree` - `/search?q=&scope=heading\|text\|all` - `GET /nodes/{id}` - `GET /nodes/{id}/changes?compare_to=` |
| Selection | `POST /selections` - `GET /selections/{id}` |
| Generate | `POST /selections/{id}/generate` (`{"force": true}` for a new generation) |
| Retrieve | `GET /selections/{id}/test-cases` - `GET /nodes/{id}/test-cases` - `GET /generations/{id}` |

## Project layout

```
app/parser/      PDF -> tree (layout-aware)      app/matching.py   cross-version identity
app/diffing.py   change classification           app/staleness.py  retrieval-time staleness
app/llm/         prompt, providers, validation   app/stores/       generation store (JSON / Mongo)
app/services/    ingest, queries, selection, generation          app/routers/  HTTP endpoints
tests/           82 tests                        scripts/demo.py   end-to-end demo
```