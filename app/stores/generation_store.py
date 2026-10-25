"""NoSQL store for LLM output.

Why a document store? A generation is a nested, variable-shape, write-once artefact (raw replies,
attempts, rejected items, evidence, source snapshots). We never join on it, and its schema will
change as prompts evolve. The relational DB keeps what needs integrity (tree, versions,
selections); this store keeps what needs flexibility.

Two interchangeable backends:
  JsonFileStore - default. One JSON file per generation, atomic write (tmp + os.replace),
                  process lock. Zero infrastructure, human-inspectable.
  MongoStore    - GENERATION_STORE=mongo MONGO_URI=... (indexes created on start).
Generations are IMMUTABLE once written; "latest" is decided by created_at, never by mutation.
"""
from __future__ import annotations
import json
import os
import threading
from pathlib import Path
from typing import Protocol


class GenerationStore(Protocol):
    def insert(self, doc: dict) -> None: ...
    def get(self, generation_id: str) -> dict | None: ...
    def find(self, *, selection_id: int | None = None, logical_id: int | None = None,
             node_id: int | None = None, fingerprint: str | None = None,
             statuses: tuple[str, ...] | None = None) -> list[dict]: ...


def _matches(d: dict, selection_id, logical_id, node_id, fingerprint, statuses) -> bool:
    if selection_id is not None and d.get("selection_id") != selection_id:
        return False
    if fingerprint is not None and d.get("fingerprint") != fingerprint:
        return False
    if statuses is not None and d.get("status") not in statuses:
        return False
    if logical_id is not None and not any(s["logical_id"] == logical_id for s in d.get("sources", [])):
        return False
    if node_id is not None and not any(s["node_id"] == node_id for s in d.get("sources", [])):
        return False
    return True


class JsonFileStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, gid: str) -> Path:
        if not gid.replace("-", "").isalnum():
            raise ValueError("bad generation id")          # no path traversal via ids
        return self.dir / f"{gid}.json"

    def insert(self, doc: dict) -> None:
        p = self._path(doc["id"])
        tmp = p.with_suffix(".tmp")
        with self._lock:
            if p.exists():
                raise FileExistsError(doc["id"])            # immutable
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)

    def get(self, generation_id: str) -> dict | None:
        try:
            p = self._path(generation_id)
        except ValueError:
            return None
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def find(self, *, selection_id=None, logical_id=None, node_id=None, fingerprint=None, statuses=None):
        out = []
        for p in self.dir.glob("*.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            if _matches(d, selection_id, logical_id, node_id, fingerprint, statuses):
                out.append(d)
        return sorted(out, key=lambda d: d["created_at"], reverse=True)


class MongoStore:
    def __init__(self, uri: str, db: str = "ct200", collection: str = "generations"):
        from pymongo import ASCENDING, MongoClient
        self.col = MongoClient(uri)[db][collection]
        self.col.create_index([("id", ASCENDING)], unique=True)
        self.col.create_index([("selection_id", ASCENDING), ("created_at", -1)])
        self.col.create_index([("sources.logical_id", ASCENDING)])
        self.col.create_index([("sources.node_id", ASCENDING)])
        self.col.create_index([("fingerprint", ASCENDING), ("status", ASCENDING)])

    def insert(self, doc: dict) -> None:
        self.col.insert_one(dict(doc))

    def get(self, generation_id: str) -> dict | None:
        return self.col.find_one({"id": generation_id}, {"_id": 0})

    def find(self, *, selection_id=None, logical_id=None, node_id=None, fingerprint=None, statuses=None):
        q: dict = {}
        if selection_id is not None:
            q["selection_id"] = selection_id
        if logical_id is not None:
            q["sources.logical_id"] = logical_id
        if node_id is not None:
            q["sources.node_id"] = node_id
        if fingerprint is not None:
            q["fingerprint"] = fingerprint
        if statuses is not None:
            q["status"] = {"$in": list(statuses)}
        return list(self.col.find(q, {"_id": 0}).sort("created_at", -1))


def make_store() -> GenerationStore:
    if os.getenv("GENERATION_STORE", "json").lower() == "mongo":
        return MongoStore(os.environ["MONGO_URI"])
    return JsonFileStore(os.getenv("GENERATION_DIR", "var/generations"))