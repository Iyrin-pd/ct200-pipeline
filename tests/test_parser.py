"""Explicit tests for every structural irregularity in the CT-200 manual."""
import pytest
from tests.helpers import doc, node
from app.textnorm import content_hash, normalize


@pytest.mark.parametrize("v", [1, 2])
def test_duplicate_heading_error_codes_are_two_distinct_nodes_with_correct_parents(v):
    d = doc(v)
    dups = [n for n in d.nodes if n.heading == "Error Codes"]
    assert len(dups) == 2
    a, b = dups
    assert a.key != b.key and a.content_hash != b.content_hash
    assert (a.number, a.parent.number, a.parent.heading) == ("4.2", "4", "Alarms and Safety Behavior")
    assert (b.number, b.parent.number, b.parent.heading) == ("7.1", "7", "Troubleshooting")
    assert any(w.code == "duplicate_heading" for w in a.warnings)
    assert b in b.parent.children and b not in a.parent.children


@pytest.mark.parametrize("v", [1, 2])
def test_skipped_level_2_1_1_1_attaches_to_2_1_and_is_flagged(v):
    d = doc(v)
    n = node(d, "2.1.1.1")
    assert n.parent.number == "2.1"
    assert n.numbering_depth == 4 and n.level == 3
    assert [w.code for w in n.warnings if w.code == "skipped_levels"]
    assert n in node(d, "2.1").children
    assert "250" in n.body_text or "300" in n.body_text


@pytest.mark.parametrize("v", [1, 2])
def test_out_of_order_3_4_before_3_3_keeps_document_order_and_parent(v):
    d = doc(v)
    sec3 = node(d, "3")
    assert [c.number for c in sec3.children] == ["3.1", "3.2", "3.4", "3.3"]
    assert node(d, "3.3").parent is sec3 and node(d, "3.4").parent is sec3
    assert any(w.code == "out_of_order" for w in node(d, "3.3").warnings)
    assert "Auto Shutoff" == node(d, "3.4").heading and "automatically powers off" in node(d, "3.4").body_text
    assert "Hypertensive Crisis" not in node(d, "3.4").body_text


@pytest.mark.parametrize("v", [1, 2])
def test_numbered_classification_list_is_a_list_not_headings(v):
    d = doc(v)
    n = node(d, "3.3")
    lists = [b for b in n.blocks if b.type == "list"]
    assert len(lists) == 1 and len(lists[0].items) == 5 and lists[0].ordered
    assert lists[0].items[4].endswith("recommends seeking immediate medical attention")
    top = [c.heading for c in d.root.children]
    assert "Normal" not in top and not any(h.startswith("Hypertens") for h in top)
    assert [c.number for c in d.root.children] == [str(i) for i in range(1, 9)]
    assert len([x for x in d.nodes if x.number == "4"]) == 1


@pytest.mark.parametrize("v,rows", [(1, 5), (2, 6)])
def test_tables_extracted_once_with_multiline_cells_merged(v, rows):
    d = doc(v)
    t = [b for b in node(d, "4.2").blocks if b.type == "table"]
    assert len(t) == 1
    assert t[0].header == ["Code", "Meaning", "Device Behavior"]
    assert len(t[0].rows) == rows
    assert t[0].rows[0] == ["E1", "Cuff not connected or leak detected", "Aborts measurement, displays E1"]
    spec = [b for b in node(d, "2.1").blocks if b.type == "table"]
    assert len(spec) == 1 and len(spec[0].rows) == 7 and spec[0].header == ["Parameter", "Value"]
    assert ["Power source", "4x AA batteries or 6V DC adapter"] in spec[0].rows
    assert not [n for n in d.nodes if n.heading in ("Parameter Value", "Code Meaning Device Behavior")]


@pytest.mark.parametrize("v", [1, 2])
def test_headings_stranded_at_page_bottom_are_kept_with_children(v):
    d = doc(v)
    for num, first_child in [("2", "2.1"), ("4", "4.1")]:
        n = node(d, num)
        assert n.blocks == [] and n.children[0].number == first_child
        assert not any(w.code == "empty_section" for w in n.warnings)


@pytest.mark.parametrize("v", [1, 2])
def test_paragraph_split_across_pages_is_joined_with_a_space(v):
    t = node(doc(v), "3.1")
    assert "to power on the device. Use the profile button" in t.body_text
    assert (t.page_start, t.page_end) == (2, 3)


def test_title_wrapped_over_three_lines_is_one_title_and_root_text():
    d = doc(1)
    assert d.title == "CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual"
    assert d.root.level == 0 and [c.number for c in d.root.children][:2] == ["1", "2"]


def test_nonbreaking_hyphen_does_not_change_content_hash_but_is_kept_raw():
    assert normalize("CT\u2011200") == "CT-200"
    assert content_hash("A", "low\u2011battery") == content_hash("A", "low-battery")
    assert "\u2011" in node(doc(1), "2.1.1.1").body_text


def test_cross_references_are_captured_not_mistaken_for_headings():
    d = doc(1)
    assert node(d, "3.3").cross_refs == ["2.1", "4.3"]
    assert node(d, "7.1").cross_refs == ["4.2"]
    assert len([n for n in d.nodes if n.number == "4.3"]) == 1


@pytest.mark.parametrize("v", [1, 2])
def test_no_content_silently_lost_or_invented(v):
    d = doc(v)
    assert [w for w in d.warnings if w.code in ("content_loss", "content_added")] == []


def test_parent_child_links_are_consistent_everywhere():
    for v in (1, 2):
        d = doc(v)
        for n in d.nodes[1:]:
            assert n in n.parent.children and n.level == n.parent.level + 1
        assert sum(len(n.children) for n in d.nodes) == len(d.nodes) - 1


def test_v2_adds_5_3_and_changes_expected_nodes_only():
    a, b = doc(1), doc(2)
    ha = {n.number: n.content_hash for n in a.nodes[1:]}
    hb = {n.number: n.content_hash for n in b.nodes[1:]}
    assert set(hb) - set(ha) == {"5.3"}
    assert {k for k in ha if ha[k] != hb[k]} == {"2.1.1.1", "3.2", "4.2", "4.3"}