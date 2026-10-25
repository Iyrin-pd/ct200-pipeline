import pytest
from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Node
from app.services import queries
from app.services.ingest import ingest_pdf
from tests.conftest import V1, V2


@pytest.fixture()
def session():
    Session = make_session_factory(make_engine("sqlite://"))
    with Session() as s:
        yield s


def find(s, version_id, number):
    return s.scalar(select(Node).where(Node.version_id == version_id, Node.number == number))


def test_v1_baseline(session):
    v1, created = ingest_pdf(session, V1, "ct200")
    assert created and v1.version_no == 1 and v1.node_count == 28 and v1.parse_warnings == []
    sections = queries.top_level_sections(session, queries.get_document(session, "ct200"), v1)
    assert [x["number"] for x in sections] == [str(i) for i in range(1, 9)]
    assert find(session, v1.id, "2.1.1.1").parent_id == find(session, v1.id, "2.1").id


def test_v2_reingest_summary_and_logical_identity(session):
    v1, _ = ingest_pdf(session, V1, "ct200")
    v2, created = ingest_pdf(session, V2, "ct200")
    assert created and v2.version_no == 2 and v2.node_count == 29
    assert [a["number"] for a in v2.summary["added"]] == ["5.3"] and v2.summary["removed"] == []
    assert sorted(m["number"] for m in v2.summary["modified"]) == ["2.1.1.1", "3.2", "4.2", "4.3"]
    a, b, other = find(session, v1.id, "4.2"), find(session, v2.id, "4.2"), find(session, v1.id, "7.1")
    assert a.logical_id == b.logical_id and a.id != b.id and a.content_hash != b.content_hash
    assert other.logical_id != a.logical_id            # the duplicate 'Error Codes' heading stays separate


def test_identical_file_is_noop(session):
    v1, c1 = ingest_pdf(session, V1, "ct200")
    again, c2 = ingest_pdf(session, V1, "ct200")
    assert c1 and not c2 and again.id == v1.id


def test_v1_is_untouched_by_v2(session):
    v1, _ = ingest_pdf(session, V1, "ct200")
    before = find(session, v1.id, "3.2")
    text, digest = before.body_text, before.content_hash
    ingest_pdf(session, V2, "ct200")
    after = find(session, v1.id, "3.2")
    assert after.body_text == text and "40 mmHg" in text and after.content_hash == digest


def test_node_changes_and_search(session):
    v1, _ = ingest_pdf(session, V1, "ct200")
    ingest_pdf(session, V2, "ct200")
    ch = queries.node_changes(session, find(session, v1.id, "4.2"))
    assert ch["changed"] and ch["diff"]["change_class"] == "material"
    assert ch["diff"]["numeric_changes"][0]["old"] == "2 seconds" and [h["version_no"] for h in ch["history"]] == [1, 2]
    assert queries.node_changes(session, find(session, v1.id, "3.3"))["changed"] is False
    doc = queries.get_document(session, "ct200")
    hits = queries.search(session, doc, v1, "low-battery", "text")        # ASCII '-' finds the U+2011 hyphen
    assert [h["number"] for h in hits] == ["2.1.1.1"]
    heads = queries.search(session, doc, v1, "error codes", "heading")
    assert sorted(h["number"] for h in heads) == ["4.2", "7.1"]