"""Generation + retrieval.

Idempotency policy:
  Key = sha256(selection fingerprint | prompt version | provider | model).
  * Same key + an existing succeeded/partial generation -> return it, no LLM call (HTTP 200,
    reused=true). LLM calls cost money and are non-deterministic; two identical requests should
    not silently yield two different 'truths'.
  * force=true -> always generate a NEW one; older generations are kept (immutable history),
    the newest is listed first.
  * FAILED generations are stored for audit but never reused: a retry after a failure tries again.
  * Changing prompt/model changes the key, so improved prompts naturally produce fresh results.
  A per-key lock makes concurrent identical requests collapse into one LLM call.
"""
from __future__ import annotations
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import config
from app.llm import pipeline
from app.llm.client import LLMClient
from app.llm.prompt import MAX_CASES, MIN_CASES, PROMPT_VERSION
from app.models import Node
from app.services.errors import Unprocessable
from app.services.queries import get_node, latest_nodes_by_logical
from app.services.selection import get_selection, sources_for
from app.staleness import assess_generation
from app.stores.generation_store import GenerationStore
from app.textnorm import sha256

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def generation_fingerprint(selection_fp: str, client: LLMClient) -> str:
    return sha256(f"{selection_fp}|{PROMPT_VERSION}|{client.provider}|{client.model}")


def generate(session: Session, store: GenerationStore, client: LLMClient, selection_id: int,
             force: bool = False) -> tuple[dict, bool]:
    """-> (generation_doc, created). A doc with status 'failed' is returned (not raised) so the API
    layer can decide the HTTP status and still show the audit trail."""
    sel = get_selection(session, selection_id)
    sources = sources_for(session, sel)
    total = sum(len(s["text"]) for s in sources)
    if total < config.MIN_SOURCE_CHARS:
        raise Unprocessable(
            f"selected sections contain only {total} characters of body text (minimum {config.MIN_SOURCE_CHARS}); "
            f"section headings alone cannot support {MIN_CASES} grounded test cases. "
            f"Select leaf sections or create the selection with include_descendants=true.")
    fp = generation_fingerprint(sel.fingerprint, client)
    with _lock_for(fp):
        if not force:
            cached = store.find(fingerprint=fp, statuses=("succeeded", "partial"))
            if cached:
                return cached[0], False
        result = pipeline.run(client, sources)
        doc = {"id": uuid.uuid4().hex, "selection_id": sel.id, "document_id": sel.document_id,
               "selection_fingerprint": sel.fingerprint, "fingerprint": fp, "status": result.status,
               "created_at": datetime.now(timezone.utc).isoformat(), "provider": client.provider,
               "model": client.model, "prompt_version": PROMPT_VERSION, "forced": force,
               "limits": {"min_cases": MIN_CASES, "max_cases": MAX_CASES},
               "sources": [{k: s[k] for k in ("node_id", "logical_id", "version_no", "number", "heading",
                                              "content_hash", "text")} for s in sources],
               "test_cases": result.cases, "rejected": result.rejected, "notes": result.notes,
               "attempts": result.attempts, "error": result.error}
        store.insert(doc)
    return doc, True


def present(session: Session, gen: dict, include_raw: bool, only_logical: int | None = None) -> dict:
    g = dict(gen)
    if only_logical is not None:
        node_logical = {s["node_id"]: s["logical_id"] for s in g["sources"]}
        g["test_cases"] = [c for c in g["test_cases"]
                           if any(node_logical.get(n) == only_logical for n in c["source_node_ids"])]
    current = latest_nodes_by_logical(session, g["document_id"], [s["logical_id"] for s in g["sources"]])
    out = assess_generation(g, current)
    if not include_raw:
        out["attempts"] = [{k: v for k, v in a.items() if k != "raw"} for a in out.get("attempts", [])]
    out["generated_from_versions"] = sorted({s["version_no"] for s in out["sources"]})
    return out


def get_generation(session: Session, store: GenerationStore, generation_id: str, include_raw: bool = False) -> dict | None:
    g = store.get(generation_id)
    return present(session, g, include_raw) if g else None


def for_selection(session: Session, store: GenerationStore, selection_id: int, include_raw: bool = False) -> dict:
    get_selection(session, selection_id)                                   # 404 if unknown
    gens = [present(session, g, include_raw) for g in store.find(selection_id=selection_id)]
    _mark_latest(gens)
    return {"selection_id": selection_id, "count": len(gens), "generations": gens}


def for_node(session: Session, store: GenerationStore, node_id: int, include_raw: bool = False) -> dict:
    node: Node = get_node(session, node_id)
    # logical id => finds generations made from ANY version of this section
    gens = [present(session, g, include_raw, only_logical=node.logical_id)
            for g in store.find(logical_id=node.logical_id)]
    _mark_latest(gens)
    return {"node_id": node.id, "logical_id": node.logical_id, "count": len(gens), "generations": gens}


def _mark_latest(gens: list[dict]) -> None:
    seen = False
    for g in gens:                                                         # already newest-first
        g["is_latest_usable"] = (not seen) and g["status"] in ("succeeded", "partial")
        seen = seen or g["is_latest_usable"]