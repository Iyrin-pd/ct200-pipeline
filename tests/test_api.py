"""End-to-end API tests (FastAPI TestClient, in-memory SQLite, temp JSON store, mocked/scripted LLM)."""
import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.deps import get_llm, get_session, get_store
from app.llm.client import MockClient, ScriptedClient
from app.main import app
from app.stores.generation_store import JsonFileStore


@pytest.fixture()
def env(tmp_path):
    engine = make_engine("sqlite://")
    factory = make_session_factory(engine)

    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    holder = {"llm": MockClient()}
    store = JsonFileStore(str(tmp_path / "gen"))
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: holder["llm"]
    yield TestClient(app), holder, store
    app.dependency_overrides.clear()


def ingest(c, pdf):
    r = c.post("/documents/ct200/versions", json={"path": pdf})
    assert r.status_code in (200, 201), r.text
    return r.json()


def nid(c, number, version="latest"):
    tree = c.get("/documents/ct200/tree", params={"version": version}).json()["tree"]
    found = []

    def walk(n):
        if n["number"] == number:
            found.append(n["id"])
        for k in n["children"]:
            walk(k)
    walk(tree)
    assert len(found) == 1, (number, found)
    return found[0]


# ------------------------------------------------------------------------------- ingestion / versioning
def test_ingest_v1_builds_tree_with_hashes_and_parents(env):
    c, *_ = env
    v = ingest(c, "ct200_manual.pdf")
    assert v["version_no"] == 1 and v["node_count"] == 28 and v["parse_warnings"] == []
    secs = c.get("/documents/ct200/sections").json()
    assert [s["number"] for s in secs["sections"]] == [str(i) for i in range(1, 9)]
    n = c.get(f"/nodes/{nid(c, '2.1.1.1')}").json()
    assert n["breadcrumbs"][-1]["number"] == "2.1" and len(n["content_hash"]) == 64
    e1, e2 = nid(c, "4.2"), nid(c, "7.1")
    assert e1 != e2
    assert c.get(f"/nodes/{e1}").json()["parent_id"] != c.get(f"/nodes/{e2}").json()["parent_id"]


def test_reingest_v2_keeps_v1_intact_and_matches_logical_nodes(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    v1_text = c.get(f"/nodes/{nid(c, '3.2', 1)}").json()["body_text"]
    v2 = ingest(c, "ct200_manual_v2.pdf")
    assert v2["version_no"] == 2
    s = v2["summary"]
    assert [a["number"] for a in s["added"]] == ["5.3"] and s["removed"] == []
    assert sorted(m["number"] for m in s["modified"]) == ["2.1.1.1", "3.2", "4.2", "4.3"]
    assert c.get(f"/nodes/{nid(c, '3.2', 1)}").json()["body_text"] == v1_text and "40 mmHg" in v1_text
    a, b = c.get(f"/nodes/{nid(c, '4.2', 1)}").json(), c.get(f"/nodes/{nid(c, '4.2', 2)}").json()
    assert a["logical_id"] == b["logical_id"] and a["id"] != b["id"] and a["content_hash"] != b["content_hash"]
    assert c.get(f"/nodes/{nid(c, '7.1', 1)}").json()["logical_id"] != a["logical_id"]
    assert len(c.get("/documents/ct200/versions").json()) == 2


def test_reingesting_identical_file_is_a_noop(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    r = c.post("/documents/ct200/versions", json={"path": "ct200_manual.pdf"})
    assert r.status_code == 200 and r.json()["created"] is False
    assert len(c.get("/documents/ct200/versions").json()) == 1


def test_path_traversal_and_missing_files_are_rejected(env):
    c, *_ = env
    assert c.post("/documents/x/versions", json={"path": "../../etc/passwd"}).status_code == 422
    assert c.post("/documents/x/versions", json={"path": "nope.pdf"}).status_code == 404


# ------------------------------------------------------------------------------- browse
def test_node_changes_endpoint(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    ingest(c, "ct200_manual_v2.pdf")
    r = c.get(f"/nodes/{nid(c, '4.2', 1)}/changes").json()
    assert r["changed"] and r["diff"]["change_class"] == "material"
    assert any(x["old"].startswith("2 seconds") and x["new"].startswith("1.5 seconds") for x in r["diff"]["numeric_changes"])
    assert [h["version_no"] for h in r["history"]] == [1, 2]
    assert c.get(f"/nodes/{nid(c, '3.3', 1)}/changes").json()["changed"] is False
    assert c.get(f"/nodes/{nid(c, '3.3', 2)}/changes").json()["status"] == "same_version"
    parent = c.get(f"/nodes/{nid(c, '4', 1)}/changes").json()
    assert parent["changed"] is False and parent["subtree_changed"] is True      # own text same, descendants changed
    added = c.get(f"/nodes/{nid(c, '5.3', 2)}/changes").json()
    assert added["change_status_in_this_version"] == "added"


def test_search_is_normalisation_aware_and_version_scoped(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    ingest(c, "ct200_manual_v2.pdf")

    def q(**p):
        return c.get("/documents/ct200/search", params=p).json()
    assert [h["number"] for h in q(q="low-battery", version=1)["results"]] == ["2.1.1.1"]
    assert [h["number"] for h in q(q="1.5 seconds", scope="text")["results"]] == ["4.2"]
    assert q(q="1.5 seconds", scope="text", version=1)["count"] == 0
    assert sorted(h["number"] for h in q(q="error codes", scope="heading")["results"]) == ["4.2", "7.1"]
    assert c.get("/documents/ct200/search", params={"q": "  "}).status_code == 422


# ------------------------------------------------------------------------------- selections
def test_selection_is_version_pinned_and_survives_reingest(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    ids = [nid(c, "3.2"), nid(c, "4.2")]
    sel = c.post("/selections", json={"name": "inflation + errors", "node_ids": ids}).json()
    ingest(c, "ct200_manual_v2.pdf")
    got = c.get(f"/selections/{sel['id']}").json()
    texts = {i["number"]: i for i in got["items"]}
    assert "40 mmHg" in texts["3.2"]["text"] and "2 seconds" in texts["4.2"]["text"]      # exact v1 text
    assert got["version_no"] == 1 and all(i["latest_state"] == "changed_in_latest" for i in got["items"])


def test_selection_policies_dedupe_mixed_versions_root_missing(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    ids = [nid(c, "3.2"), nid(c, "4.2")]
    a = c.post("/selections", json={"name": "A", "node_ids": ids})
    b = c.post("/selections", json={"name": "B", "node_ids": ids[::-1] + ids})
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["id"] == a.json()["id"] and b.json()["deduplicated"] and b.json()["requested_name_ignored"]
    c2 = c.post("/selections", json={"name": "A", "node_ids": [ids[0]]})
    assert c2.status_code == 201 and c2.json()["id"] != a.json()["id"]                    # same name, different set
    ingest(c, "ct200_manual_v2.pdf")
    mixed = c.post("/selections", json={"name": "m", "node_ids": [ids[0], nid(c, "4.2", 2)]})
    assert mixed.status_code == 422
    assert c.post("/selections", json={"name": "r", "node_ids": [1]}).status_code == 422   # id 1 is the title root
    assert c.post("/selections", json={"name": "x", "node_ids": [99999]}).status_code == 404
    assert c.post("/selections", json={"name": "", "node_ids": ids}).status_code == 422
    assert c.post("/selections", json={"name": "x", "node_ids": []}).status_code == 422
    d = c.post("/selections", json={"name": "tree", "node_ids": [nid(c, "4", 2)], "include_descendants": True}).json()
    assert [i["number"] for i in d["items"]] == ["4", "4.1", "4.2", "4.3"]


# ------------------------------------------------------------------------------- generation + staleness
def test_generate_idempotency_force_and_staleness_after_reingest(env):
    c, *_ = env
    ingest(c, "ct200_manual.pdf")
    sel = c.post("/selections", json={"name": "s", "node_ids": [nid(c, "3.2"), nid(c, "4.2")]}).json()
    g1 = c.post(f"/selections/{sel['id']}/generate")
    assert g1.status_code == 201 and 3 <= len(g1.json()["test_cases"]) <= 5
    assert g1.json()["staleness_summary"]["all_current"] is True
    g2 = c.post(f"/selections/{sel['id']}/generate")
    assert g2.status_code == 200 and g2.json()["reused"] and g2.json()["id"] == g1.json()["id"]
    g3 = c.post(f"/selections/{sel['id']}/generate", json={"force": True})
    assert g3.status_code == 201 and g3.json()["id"] != g1.json()["id"]

    ingest(c, "ct200_manual_v2.pdf")                                       # the manual changes
    r = c.get(f"/selections/{sel['id']}/test-cases").json()
    assert r["count"] == 2 and r["generations"][0]["is_latest_usable"] is True
    gen = r["generations"][0]
    assert gen["staleness_summary"]["all_current"] is False
    stale = [t for t in gen["test_cases"] if t["staleness"]["status"] != "current"]
    assert stale and all(t["staleness"]["reasons"] for t in stale)
    # the same data is reachable from a node id of the NEW version (same logical node)
    byn = c.get(f"/nodes/{nid(c, '4.2', 2)}/test-cases").json()
    assert byn["count"] == 2

    # a selection over sections that did NOT change keeps its tests current after the re-ingest
    sel2 = c.post("/selections", json={"name": "untouched", "node_ids": [nid(c, "5.1", 1), nid(c, "5.2", 1)]}).json()
    g = c.post(f"/selections/{sel2['id']}/generate")
    assert g.status_code == 201 and g.json()["staleness_summary"]["all_current"] is True


def test_insufficient_content_is_refused_before_calling_the_llm(env):
    c, holder, _ = env
    holder["llm"] = ScriptedClient([])
    ingest(c, "ct200_manual.pdf")
    sel = c.post("/selections", json={"name": "heading only", "node_ids": [nid(c, "4")]}).json()
    r = c.post(f"/selections/{sel['id']}/generate")
    assert r.status_code == 422 and "include_descendants" in r.json()["message"]
    assert holder["llm"].calls == []


def test_malformed_llm_output_gives_502_and_stored_audit_but_no_cases_and_is_not_reused(env):
    c, holder, store = env
    ingest(c, "ct200_manual.pdf")
    sel = c.post("/selections", json={"name": "s", "node_ids": [nid(c, "3.2"), nid(c, "4.2")]}).json()
    holder["llm"] = ScriptedClient(["Sorry, I cannot do that.", "{not json"])
    r = c.post(f"/selections/{sel['id']}/generate")
    assert r.status_code == 502 and r.json()["generation"]["test_cases"] == []
    stored = store.find(selection_id=sel["id"])
    assert len(stored) == 1 and stored[0]["status"] == "failed" and stored[0]["attempts"][0]["raw"].startswith("Sorry")
    holder["llm"] = MockClient()                                           # failures are never cached
    ok = c.post(f"/selections/{sel['id']}/generate")
    assert ok.status_code == 201 and ok.json()["status"] == "succeeded"