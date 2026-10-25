from tests.helpers import doc, node
from app.staleness import assess_case, assess_generation


def snap(n, nid):
    return {"node_id": nid, "logical_id": n.number, "version_no": 1, "number": n.number, "heading": n.heading,
            "content_hash": n.content_hash, "text": n.body_text}


def cur(d, numbers):
    out = {}
    for num in numbers:
        ns = [x for x in d.nodes if x.number == num]
        out[num] = None if not ns else {"node_id": 100 + int(num.replace(".", "")), "version_no": 2, "number": num,
                                       "heading": ns[0].heading, "text": ns[0].body_text, "content_hash": ns[0].content_hash}
    return out


def tc(nid, quote, title="t", expected="e", steps=("s",), **kw):
    return {"id": "x", "title": title, "type": "boundary", "priority": "high", "preconditions": [],
            "steps": list(steps), "expected_result": expected, "source_node_ids": [nid],
            "evidence": [{"node_id": nid, "quote": quote}], **kw}


V1, V2 = doc(1), doc(2)
N = {num: node(V1, num) for num in ("3.2", "3.3", "4.2", "5.2", "2.1.1.1")}
GEN = {"sources": [snap(N[k], i) for i, k in enumerate(N, start=1)]}
ID = {k: i for i, k in enumerate(N, start=1)}
CUR = cur(V2, list(N))


def test_unchanged_node_is_current():
    r = assess_case(tc(ID["3.3"], "Normal: systolic < 120 and diastolic < 80"), GEN, CUR)
    assert r["status"] == "current" and r["is_current"] and r["reasons"] == []


def test_threshold_changed_and_quote_gone_is_stale_material_with_numeric_reason():
    c = tc(ID["3.2"], "device inflates in 40 mmHg increments up to a maximum of 299 mmHg",
           title="Verify 40 mmHg inflation step", expected="Cuff inflates in 40 mmHg steps")
    r = assess_case(c, GEN, CUR)
    assert r["status"] == "stale" and r["severity"] == "material"
    assert "40" in r["per_node"][0]["drifted_values"] or any(n["old"].startswith("40") for n in r["per_node"][0]["numeric_changes"])


def test_quote_intact_but_neighbouring_threshold_changed_is_possibly_stale_not_current():
    c = tc(ID["3.2"], "inflates the cuff to an initial target of 180 mmHg", title="Initial target 180")
    r = assess_case(c, GEN, CUR)
    assert r["per_node"][0]["evidence_intact"] is True
    assert r["status"] == "possibly_stale" and r["severity"] == "material"


def test_table_row_value_change_detected_for_the_row_it_cites():
    c = tc(ID["4.2"], "Auto-deflates within 2 seconds", title="E3 deflation time", expected="Deflates in 2 seconds")
    assert assess_case(c, GEN, CUR)["status"] == "stale"
    other = tc(ID["4.2"], "Cuff not connected or leak detected", title="E1 shown", expected="E1 displayed")
    assert assess_case(other, GEN, CUR)["status"] == "possibly_stale"       # row intact, table changed elsewhere


def test_unchanged_sibling_section_stays_current_even_though_document_changed():
    assert assess_case(tc(ID["5.2"], "no manual \"sync now\" trigger"), GEN, CUR)["status"] == "current"


def test_removed_source_node_is_reported_as_source_removed():
    cur2 = dict(CUR)
    cur2["3.2"] = None
    r = assess_case(tc(ID["3.2"], "initial target of 180 mmHg"), GEN, cur2)
    assert r["status"] == "source_removed"


def test_wording_only_change_is_review_suggested_not_stale_and_not_current():
    old = node(V1, "3.3")
    src = {**snap(old, 1), "text": "The unit is small and light. Systolic above 180 means crisis."}
    new = {"node_id": 9, "version_no": 2, "number": "3.3", "heading": old.heading,
           "text": "The device is tiny and light. Systolic above 180 means crisis.", "content_hash": "different"}
    c = tc(1, "Systolic above 180 means crisis", title="Crisis at 180", expected="Crisis flagged above 180")
    r = assess_case(c, {"sources": [src]}, {"3.3": new})
    assert r["status"] == "review_suggested" and r["severity"] == "wording"


def test_cosmetic_only_change_collapses_to_current():
    old = node(V1, "3.3")
    new = {"node_id": 9, "version_no": 2, "number": "3.3", "heading": old.heading,
           "text": old.body_text.replace("Normal", "normal"), "content_hash": "different-hash"}
    r = assess_case(tc(ID["3.3"], "systolic < 120 and diastolic < 80"), GEN, {"3.3": new})
    assert r["status"] == "current" and "cosmetic" in r["reasons"][0]


def test_worst_node_wins_when_a_case_cites_several_sections():
    c = tc(ID["3.3"], "Normal: systolic < 120 and diastolic < 80")
    c["source_node_ids"] = [ID["3.3"], ID["4.2"]]
    c["evidence"].append({"node_id": ID["4.2"], "quote": "Auto-deflates within 2 seconds"})
    r = assess_case(c, GEN, CUR)
    assert r["status"] == "stale" and len(r["per_node"]) == 2


def test_generation_rollup_counts_and_does_not_mutate_input():
    gen = {**GEN, "test_cases": [tc(ID["3.3"], "Normal: systolic < 120 and diastolic < 80", title="a"),
                                 tc(ID["4.2"], "Auto-deflates within 2 seconds", title="b")]}
    before = repr(gen)
    out = assess_generation(gen, CUR)
    assert repr(gen) == before and "staleness" not in gen["test_cases"][0]
    assert out["staleness_summary"]["counts"] == {"current": 1, "stale": 1}
    assert out["staleness_summary"]["overall"] == "stale" and not out["staleness_summary"]["all_current"]