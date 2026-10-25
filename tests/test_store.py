import tempfile
from app.stores.generation_store import JsonFileStore


def g(i, sel=1, lid=7, status="succeeded", fp="f"):
    return {"id": f"gen-{i}", "selection_id": sel, "status": status, "fingerprint": fp,
            "created_at": f"2026-01-0{i}T00:00:00Z", "sources": [{"node_id": i, "logical_id": lid}], "test_cases": []}


def test_roundtrip_filters_and_newest_first():
    s = JsonFileStore(tempfile.mkdtemp())
    s.insert(g(1))
    s.insert(g(2, status="failed"))
    s.insert(g(3, sel=2, lid=9))
    assert s.get("gen-1")["id"] == "gen-1" and s.get("nope") is None
    assert [d["id"] for d in s.find(selection_id=1)] == ["gen-2", "gen-1"]
    assert [d["id"] for d in s.find(logical_id=9)] == ["gen-3"]
    assert [d["id"] for d in s.find(selection_id=1, statuses=("succeeded", "partial"))] == ["gen-1"]
    assert s.find(fingerprint="zzz") == []


def test_generations_are_immutable_and_ids_cannot_traverse_paths():
    s = JsonFileStore(tempfile.mkdtemp())
    s.insert(g(1))
    try:
        s.insert(g(1))
        assert False, "should refuse overwrite"
    except FileExistsError:
        pass
    assert s.get("../../etc/passwd") is None