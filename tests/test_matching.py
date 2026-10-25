from tests.helpers import doc, node
from tests.synthetic import build, find
from app.matching import match_documents, classify


def test_real_v1_to_v2_exactly_the_expected_changes():
    a, b = doc(1), doc(2)
    r = match_documents(a, b)
    status = {n.number: classify(r.matches.get(n.key)) for n in b.nodes[1:]}
    assert [k for k, v in status.items() if v == "added"] == ["5.3"]
    assert sorted(k for k, v in status.items() if v == "modified") == ["2.1.1.1", "3.2", "4.2", "4.3"]
    assert r.removed == []
    assert len(r.matches) == len(a.nodes)


def test_real_duplicate_error_codes_never_cross_matched():
    a, b = doc(1), doc(2)
    r = match_documents(a, b)
    assert r.old_for(node(b, "4.2").key) is node(a, "4.2")
    assert r.old_for(node(b, "7.1").key) is node(a, "7.1")


def test_real_out_of_order_nodes_match_by_number_not_position():
    a, b = doc(1), doc(2)
    r = match_documents(a, b)
    assert r.old_for(node(b, "3.3").key) is node(a, "3.3")
    assert r.old_for(node(b, "3.4").key) is node(a, "3.4")
    assert r.matches[node(b, "3.3").key].method == "number+heading"


def test_section_inserted_before_renumbers_everything_but_identity_survives():
    old = build([("1", "Intro", "alpha beta gamma", []), ("2", "Specs", "pressure range is 299 mmHg", [])])
    new = build([("1", "Intro", "alpha beta gamma", []), ("2", "Safety", "new safety text here", []),
                 ("3", "Specs", "pressure range is 299 mmHg", [])])
    r = match_documents(old, new)
    m = r.matches[find(new, "Specs").key]
    assert m.old is find(old, "Specs") and m.method == "heading(renumbered)"
    assert [n.heading for n in r.added] == ["Safety"] and r.removed == []


def test_retitled_section_with_same_number_still_matches_and_is_modified():
    old = build([("1", "Alarm Thresholds", "audible alarms are limited to E1 errors", [])])
    new = build([("1", "Alarm Settings", "audible alarms are limited to E1 errors", [])])
    r = match_documents(old, new)
    m = r.matches[new.nodes[1].key]
    assert m.method in ("number+similarity(retitled)", "fuzzy_heading", "body_similarity")
    assert classify(m) == "modified"


def test_moved_section_matched_globally_with_children_followed():
    old = build([("1", "A", "x", [("1.1", "Moving", "the quick brown fox jumps over the lazy dog", [("1.1.1", "Kid", "kid text", [])])]),
                 ("2", "B", "y", [])])
    new = build([("1", "A", "x", []),
                 ("2", "B", "y", [("2.1", "Moving", "the quick brown fox jumps over the lazy dog", [("2.1.1", "Kid", "kid text", [])])])])
    r = match_documents(old, new)
    assert r.matches[find(new, "Moving").key].method == "moved"
    assert r.matches[find(new, "Kid").key].old is find(old, "Kid")
    assert r.removed == [] and r.added == []


def test_split_section_is_reported_as_added_and_removed_never_silently_merged():
    old = build([("1", "Everything", "part one text. part two text. part three text.", [])])
    new = build([("1", "Part One", "part one text.", []), ("2", "Part Two", "part two text.", [])])
    r = match_documents(old, new)
    assert [n.heading for n in r.removed] == ["Everything"]
    assert sorted(n.heading for n in r.added) == ["Part One", "Part Two"]


def test_two_identical_sibling_headings_are_not_guessed():
    old = build([("1", "Notes", "first note", []), ("2", "Notes", "second note", [])])
    new = build([("1", "Notes", "first note", []), ("2", "Notes", "second note edited", [])])
    r = match_documents(old, new)
    assert r.matches[new.nodes[2].key].old is old.nodes[2]
    old2 = build([("1", "Notes", "alpha alpha alpha", []), ("2", "Notes", "zeta zeta zeta", [])])
    new2 = build([("5", "Notes", "zeta zeta zeta", []), ("6", "Notes", "alpha alpha alpha", [])])
    r2 = match_documents(old2, new2)
    for n in new2.nodes[1:]:
        assert r2.matches[n.key].old.body_text == n.body_text