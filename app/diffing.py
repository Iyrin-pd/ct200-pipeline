"""Lightweight diff + change classification between two versions of one node.

change_class (ordered by severity):
  none      - normalised text identical
  cosmetic  - differs only by case / punctuation / spacing
  wording   - words changed but no number, comparator or modal/negation word changed
              and nothing substantial was added or removed
  material  - a number/unit/comparator changed, a modal or negation word changed
              (must/should/not/only/...), or >= SUBSTANTIAL_TOKENS words were added/removed

This is a LEXICAL heuristic. It cannot tell that two differently worded sentences mean the
same thing. See docs/APPROACH.md for the limits.
"""
from __future__ import annotations
import difflib
import re

from app.textnorm import normalize

SUBSTANTIAL_TOKENS = 5
# words whose appearance/disappearance flips an obligation, prohibition or bound
MODALS = {"must", "should", "shall", "may", "cannot", "can't", "not", "no", "never", "only", "always",
          "except", "unless", "without", "least", "most", "exceeds", "above", "below", "default",
          "disables", "aborts", "requires", "required", "silenced"}
_PUNCT = "\u200b.,;:!?()[]\"'|"
_HAS_DIGIT = re.compile(r"\d")
_COMPARATOR = re.compile(r"[<>≥≤±%]")


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def _t(tok: str) -> str:
    return tok.strip(_PUNCT).casefold()


def similarity(a: str, b: str) -> float:
    ta, tb = [_t(x) for x in tokens(a)], [_t(x) for x in tokens(b)]
    if not ta and not tb:
        return 1.0
    return difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()


def _is_numeric(tok: str) -> bool:
    return bool(_HAS_DIGIT.search(tok) or _COMPARATOR.search(tok))


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", normalize(text)))


def _clip(s: str, n: int = 100) -> str | None:
    s = s.strip().rstrip(".,;")
    return (s[:n] + "…" if len(s) > n else s) or None


def diff_summary(old_heading: str, old_body: str, new_heading: str, new_body: str, max_changes: int = 25) -> dict:
    old_t, new_t = tokens(old_body), tokens(new_body)
    heading_changed = normalize(old_heading) != normalize(new_heading)
    if normalize(old_body) == normalize(new_body) and not heading_changed:
        return {"changed": False, "change_class": "none", "reasons": [], "heading_changed": False,
                "numeric_changes": [], "changes": [], "stats": {"tokens_added": 0, "tokens_removed": 0}}

    sm = difflib.SequenceMatcher(None, old_t, new_t, autojunk=False)
    changes, numeric_changes, reasons = [], [], []
    added = removed = 0
    material = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        o, n = old_t[i1:i2], new_t[j1:j2]
        # pure punctuation/case flips are not real changes
        if [_t(x) for x in o] == [_t(x) for x in n]:
            continue
        removed += len(o)
        added += len(n)
        changes.append({"op": tag, "context": " ".join(old_t[max(0, i1 - 4):i1]),
                        "old": " ".join(o), "new": " ".join(n)})
        if any(_is_numeric(x) for x in o + n):
            material = True
            unit_o = old_t[i2:i2 + 1] if o else []
            unit_n = new_t[j2:j2 + 1] if n else []
            numeric_changes.append({"old": _clip(" ".join(o + unit_o)), "new": _clip(" ".join(n + unit_n))})
            reasons.append("number_or_comparator_changed" if (o and n) else
                           ("number_added" if n else "number_removed"))
        if ({_t(x) for x in o} ^ {_t(x) for x in n}) & MODALS:
            material = True
            reasons.append("modal_or_negation_changed")
        if tag in ("insert", "delete") and max(len(o), len(n)) >= SUBSTANTIAL_TOKENS:
            material = True
            reasons.append("content_added" if tag == "insert" else "content_removed")
        elif tag == "replace" and max(len(o), len(n)) >= SUBSTANTIAL_TOKENS * 2:
            material = True
            reasons.append("large_rewrite")

    if heading_changed:
        reasons.append("heading_changed")
    if material:
        klass = "material"
    elif changes or heading_changed:
        klass = "wording"
    else:
        klass = "cosmetic"          # tokens differ only by case/punctuation
    return {"changed": True, "change_class": klass, "reasons": sorted(set(reasons)),
            "heading_changed": heading_changed, "numeric_changes": numeric_changes[:max_changes],
            "changes": changes[:max_changes], "stats": {"tokens_added": added, "tokens_removed": removed},
            "truncated": len(changes) > max_changes}