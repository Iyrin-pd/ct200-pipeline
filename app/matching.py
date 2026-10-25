"""Decide which node in the NEW version is "the same logical node" as one in the OLD version.

Matching is done TOP-DOWN inside already-matched parents, so a duplicated heading
("Error Codes" at 4.2 and 7.1) can only compete with siblings, never with its namesake in
another section. Within one sibling group the passes run from strongest to weakest evidence;
each pass only accepts a UNIQUE, unambiguous candidate. A node nobody claims becomes `added`
(new side) or `removed` (old side): a false "added/removed" is preferred over a false "same".

  A  number + normalised heading   (the normal case)
  B  normalised heading            (section renumbered, e.g. a new section inserted before it)
  C  same number + similar content (heading retitled, number kept)
  D  fuzzy heading  >= 0.85        (typo fixes / small retitles)
  E  body similarity >= 0.80       (retitled AND renumbered, text barely touched)
  F  global "moved" pass           (same heading + near-identical body, different parent)
"""
from __future__ import annotations
import difflib
from dataclasses import dataclass, field

from app.diffing import similarity
from app.parser.model import ParsedDocument, ParsedNode
from app.textnorm import normalize_heading

FUZZY_HEADING = 0.85
BODY_SIM = 0.80
MOVED_BODY_SIM = 0.90
NUMBER_BODY_SIM = 0.70       # same number: body must still be mostly the same ...
NUMBER_HEADING_SIM = 0.60    # ... or the heading must be a recognisable retitle
TIE_MARGIN = 0.02


@dataclass
class Match:
    old: ParsedNode
    new: ParsedNode
    method: str
    score: float


@dataclass
class MatchResult:
    matches: dict[str, Match] = field(default_factory=dict)     # new.key -> Match
    removed: list[ParsedNode] = field(default_factory=list)      # old nodes with no counterpart
    added: list[ParsedNode] = field(default_factory=list)

    def old_for(self, new_key: str) -> ParsedNode | None:
        m = self.matches.get(new_key)
        return m.old if m else None


def _uniq_by(items, keyfn):
    buckets: dict = {}
    for x in items:
        buckets.setdefault(keyfn(x), []).append(x)
    return {k: v[0] for k, v in buckets.items() if len(v) == 1}


def _heading_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_heading(a), normalize_heading(b)).ratio()


def _unambiguous(olds, news, score, threshold):
    """Pairs scoring >= threshold that are each other's clear best (no rival within TIE_MARGIN on
    either side). Ties are refused instead of broken arbitrarily: a wrong merge is worse than
    an honest added/removed pair."""
    table = {(id(o), id(n)): score(o, n) for o in olds for n in news}
    out = []
    for o in olds:
        for n in news:
            r = table[(id(o), id(n))]
            if r < threshold:
                continue
            rivals = [table[(id(o), id(n2))] for n2 in news if n2 is not n] + \
                     [table[(id(o2), id(n))] for o2 in olds if o2 is not o]
            if all(r - x > TIE_MARGIN for x in rivals):
                out.append((r, o, n))
    return sorted(out, key=lambda t: -t[0])


def match_documents(old: ParsedDocument, new: ParsedDocument) -> MatchResult:
    res = MatchResult()
    claimed_old: set[str] = set()

    def claim(o: ParsedNode, n: ParsedNode, method: str, score: float):
        res.matches[n.key] = Match(o, n, method, score)
        claimed_old.add(o.key)

    claim(old.root, new.root, "root", 1.0)

    def match_children(op: ParsedNode, np_: ParsedNode):
        olds = [c for c in op.children if c.key not in claimed_old]
        news = [c for c in np_.children if c.key not in res.matches]
        newly: list[tuple[ParsedNode, ParsedNode]] = []

        def take(o, n, method, score):
            claim(o, n, method, score)
            olds.remove(o)
            news.remove(n)
            newly.append((o, n))

        # A: number + heading
        ko = _uniq_by(olds, lambda x: (x.number, normalize_heading(x.heading)))
        kn = _uniq_by(news, lambda x: (x.number, normalize_heading(x.heading)))
        for k in [k for k in kn if k in ko]:
            take(ko[k], kn[k], "number+heading", 1.0)
        # B: heading only (unique on both sides)
        ko = _uniq_by(olds, lambda x: normalize_heading(x.heading))
        kn = _uniq_by(news, lambda x: normalize_heading(x.heading))
        for k in [k for k in kn if k in ko]:
            take(ko[k], kn[k], "heading(renumbered)", 0.95)
        # C: same number, content similar
        ko = _uniq_by(olds, lambda x: x.number)
        kn = _uniq_by(news, lambda x: x.number)
        for k in [k for k in kn if k in ko]:
            bs = similarity(ko[k].body_text, kn[k].body_text)
            hs = _heading_ratio(ko[k].heading, kn[k].heading)
            if bs >= NUMBER_BODY_SIM or hs >= NUMBER_HEADING_SIM:
                take(ko[k], kn[k], "number+similarity(retitled)", round(max(bs, hs), 3))
        # D: fuzzy heading
        for r, o, n in _unambiguous(olds, news, lambda o, n: _heading_ratio(o.heading, n.heading), FUZZY_HEADING):
            take(o, n, "fuzzy_heading", round(r, 3))
        # E: body similarity (retitled + renumbered)
        for r, o, n in _unambiguous(olds, news,
                                    lambda o, n: similarity(o.body_text, n.body_text) if (o.body_text and n.body_text) else 0.0,
                                    BODY_SIM):
            take(o, n, "body_similarity", round(r, 3))
        for o, n in newly:
            match_children(o, n)

    match_children(old.root, new.root)

    # F: global pass for nodes whose parent changed (moved sections)
    changed = True
    while changed:
        changed = False
        uo = [n for n in old.nodes if n.key not in claimed_old]
        un = [n for n in new.nodes if n.key not in res.matches]
        ko = _uniq_by(uo, lambda x: normalize_heading(x.heading))
        kn = _uniq_by(un, lambda x: normalize_heading(x.heading))
        for k in [k for k in kn if k in ko]:
            o, n = ko[k], kn[k]
            s = similarity(o.body_text, n.body_text)
            if s >= MOVED_BODY_SIM:
                claim(o, n, "moved", round(s, 3))
                match_children(o, n)
                changed = True

    res.removed = [n for n in old.nodes if n.key not in claimed_old]
    res.added = [n for n in new.nodes if n.key not in res.matches]
    return res


def classify(m: Match | None) -> str:
    """unchanged | modified | added"""
    if m is None:
        return "added"
    return "unchanged" if m.old.content_hash == m.new.content_hash else "modified"