"""Retrieval-time staleness: does a stored test case still reflect the CURRENT document?

Inputs are plain dicts so this module is pure and fully unit-tested:
  generation["sources"]  : snapshots taken at generation time
                           {node_id, logical_id, version_no, number, heading, content_hash, text}
  current[logical_id]    : the latest-version node for each logical id, or None if it vanished
                           {node_id, version_no, number, heading, text, content_hash}

Per test case we look ONLY at the nodes it cites and answer with one of:
  current           cited node's normalised content hash is unchanged
  review_suggested  node changed, quoted evidence still verbatim, change is cosmetic/wording
  possibly_stale    node changed, quoted evidence still verbatim, but the change is MATERIAL
                    (a number, modal/negation or a whole rule/row changed elsewhere in the node)
  stale             quoted evidence is no longer present verbatim, OR a number the test relies
                    on no longer appears in the node. `severity` says material|wording
  source_removed    the logical node has no counterpart in the latest version

What this deliberately cannot do (see docs/APPROACH.md): understand meaning. A paraphrased
requirement with no number change, a change in a cross-referenced section, or a test whose
quote is still present but whose *intent* changed will not be caught.
"""
from __future__ import annotations
from app.diffing import diff_summary, numbers_in
from app.textnorm import normalize

RANK = {"current": 0, "review_suggested": 1, "possibly_stale": 2, "stale": 3, "source_removed": 4}


def _node_state(case: dict, src: dict, cur: dict | None) -> dict:
    cited = [e["quote"] for e in case.get("evidence", []) if e["node_id"] == src["node_id"]]
    base = {"node_id": src["node_id"], "logical_id": src["logical_id"], "number": src.get("number"),
            "heading": src["heading"], "generated_from_version": src["version_no"]}
    if cur is None:
        return {**base, "status": "source_removed", "severity": "material",
                "reasons": ["node no longer exists in the latest version"], "current_version": None}
    base["current_version"] = cur["version_no"]
    base["current_node_id"] = cur["node_id"]
    if cur["content_hash"] == src["content_hash"]:
        return {**base, "status": "current", "reasons": [], "severity": None}

    d = diff_summary(src["heading"], src["text"], cur["heading"], cur["text"])
    if not d["changed"]:        # hash differs only because of something normalise() ignores
        return {**base, "status": "current", "reasons": [], "severity": None}
    cur_norm = normalize(cur["text"]).casefold()
    intact = all(normalize(q).casefold() in cur_norm for q in cited)
    reasons = list(d["reasons"])

    # numbers the TEST itself relies on (and that came from the old source) but which vanished
    case_text = " ".join([case["title"], case["expected_result"], *case["steps"], *case.get("preconditions", [])])
    relied = numbers_in(case_text) & numbers_in(src["text"])
    drift = sorted(relied - numbers_in(cur["text"]))
    if drift:
        reasons.append(f"values used by this test no longer appear in the section: {', '.join(drift)}")

    out = {**base, "evidence_intact": intact, "change_class": d["change_class"],
           "numeric_changes": d["numeric_changes"], "changes": d["changes"][:6], "drifted_values": drift}
    if not intact or drift:
        sev = "material" if (drift or d["change_class"] == "material") else "wording"
        if not intact:
            reasons.append("quoted evidence no longer appears verbatim")
        return {**out, "status": "stale", "severity": sev, "reasons": reasons}
    if d["change_class"] == "cosmetic":
        return {**out, "status": "current", "severity": None, "reasons": ["cosmetic change only (case/punctuation)"]}
    if d["change_class"] == "material":
        reasons.append("cited text unchanged, but other content in the same section changed materially")
        return {**out, "status": "possibly_stale", "severity": "material", "reasons": reasons}
    return {**out, "status": "review_suggested", "severity": "wording", "reasons": reasons}


def assess_case(case: dict, generation: dict, current: dict) -> dict:
    snap = {s["node_id"]: s for s in generation["sources"]}
    states = []
    for nid in case["source_node_ids"]:
        src = snap.get(nid)
        if src is None:
            continue
        states.append(_node_state(case, src, current.get(src["logical_id"])))
    worst = max(states, key=lambda s: RANK[s["status"]], default=None)
    status = worst["status"] if worst else "current"
    return {"status": status, "severity": worst.get("severity") if worst else None,
            "is_current": status == "current",
            "reasons": sorted({r for s in states for r in s["reasons"]}),
            "per_node": states}


def assess_generation(generation: dict, current: dict) -> dict:
    """Adds `staleness` to every test case and a rollup to the generation. Does not mutate input."""
    cases = [{**tc, "staleness": assess_case(tc, generation, current)} for tc in generation.get("test_cases", [])]
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["staleness"]["status"]] = counts.get(c["staleness"]["status"], 0) + 1
    worst = max((c["staleness"]["status"] for c in cases), key=lambda s: RANK[s], default="current")
    return {**generation, "test_cases": cases,
            "staleness_summary": {"overall": worst, "counts": counts,
                                  "all_current": worst == "current",
                                  "method": "lexical: normalised hash + verbatim evidence + numeric drift (no semantic check)"}}