"""Turn raw LLM text into validated test cases - or an explicit, inspectable failure.

  1. The model's output is untrusted input. Nothing is stored as a 'test case' unless it passes
     schema validation AND the grounding check (every evidence quote must occur, after
     normalisation, in the section it cites).
  2. Be tolerant in what we ACCEPT syntactically (code fences, prose around the JSON, a
     truncated tail, case differences in enum values): cheap and safe.
  3. Be strict on what we KEEP semantically (grounding, ids, non-empty steps). A hallucinated
     quote is dropped, never "fixed".
  4. Every drop is recorded with a reason; nothing disappears silently.
"""
from __future__ import annotations
import json
import re
import uuid
from dataclasses import dataclass, field

from app.llm.prompt import MAX_CASES, MIN_CASES, TYPES
from app.textnorm import normalize

PRIORITIES = {"high", "medium", "low"}
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
QUOTE_MIN, QUOTE_MAX = 8, 300


@dataclass
class Outcome:
    cases: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    fatal: str | None = None            # unparseable / wrong shape: nothing usable

    @property
    def problems(self) -> str:
        lines = [f"- {self.fatal}"] if self.fatal else []
        lines += [f"- test case #{r['index']}: {r['reason']}" for r in self.rejected[:8]]
        if self.truncated:
            lines.append("- the reply was cut off before the JSON finished")
        return "\n".join(lines) or "- unknown problem"


# ----------------------------------------------------------------------------- JSON extraction
def _candidates(raw: str):
    raw = raw.strip()
    yield "whole", raw
    for m in _FENCE.finditer(raw):
        yield "fence", m.group(1).strip()
    for i, ch in enumerate(raw):                       # first '{' or '[' anywhere (prose before JSON)
        if ch in "{[":
            yield "embedded", raw[i:]
            break


def _salvage_cases(text: str) -> list:
    """Truncated reply: pull every COMPLETE object out of the test_cases array."""
    m = re.search(r'"test_cases"\s*:\s*\[', text)
    start = m.end() if m else (text.find("[") + 1 if text.lstrip().startswith("[") else -1)
    if start <= 0:
        return []
    dec, pos, out = json.JSONDecoder(), start, []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \n\r\t,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            break                                      # incomplete tail object: discard it
        out.append(obj)
    return out


def extract_cases(raw: str) -> tuple[list, list[str], bool]:
    """-> (list_of_raw_case_objects, notes, truncated). Raises ValueError if nothing usable."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty response")
    notes: list[str] = []
    dec = json.JSONDecoder()
    for tag, cand in _candidates(raw):
        try:
            obj, end = dec.raw_decode(cand)
        except json.JSONDecodeError:
            continue
        if tag != "whole":
            notes.append("json_extracted_from_wrapped_reply")
        if end < len(cand) and cand[end:].strip():
            notes.append("trailing_text_ignored")
        if isinstance(obj, dict):
            for key in ("test_cases", "testCases", "cases", "tests"):
                if isinstance(obj.get(key), list):
                    return obj[key], notes, False
            raise ValueError(f"JSON object has no 'test_cases' list (keys: {sorted(obj)[:6]})")
        if isinstance(obj, list):
            notes.append("bare_array_accepted")
            return obj, notes, False
        raise ValueError(f"top-level JSON is {type(obj).__name__}, expected object")
    salvaged = _salvage_cases(raw)
    if salvaged:
        notes.append(f"truncated_reply_salvaged_{len(salvaged)}_complete_cases")
        return salvaged, notes, True
    raise ValueError("no parseable JSON found in reply")


# ----------------------------------------------------------------------------- per-case validation
def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"\n+", v) if x.strip()]
    if not isinstance(v, list):
        return []
    return [normalize(str(x)) for x in v if str(x).strip()]


def _text(v) -> str:
    return normalize(v) if isinstance(v, str) else ""


def validate_case(obj, idx: int, source_text: dict[int, str]) -> tuple[dict | None, str | None]:
    """source_text: node_id -> normalised searchable text. Returns (case, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, f"not an object ({type(obj).__name__})"
    title = _text(obj.get("title"))
    if not (5 <= len(title) <= 160):
        return None, "missing/invalid title"
    ctype = _text(obj.get("type")).lower().replace(" ", "_").replace("-", "_")
    if ctype not in TYPES:
        return None, f"unknown type {obj.get('type')!r}"
    prio = _text(obj.get("priority")).lower()
    if prio not in PRIORITIES:
        return None, f"unknown priority {obj.get('priority')!r}"
    steps = _as_list(obj.get("steps"))
    if not steps:
        return None, "no steps"
    expected = _text(obj.get("expected_result"))
    if len(expected) < 5:
        return None, "missing expected_result"
    ev_raw = obj.get("evidence")
    if not isinstance(ev_raw, list) or not ev_raw:
        return None, "no evidence quotes (cannot verify grounding)"
    evidence = []
    for e in ev_raw:
        if not isinstance(e, dict):
            return None, "evidence item is not an object"
        try:
            nid = int(e.get("node_id"))
        except (TypeError, ValueError):
            return None, "evidence.node_id is not an integer"
        if nid not in source_text:
            return None, f"evidence cites node {nid} which is not in the selection"
        q = _text(e.get("quote"))
        if not (QUOTE_MIN <= len(q) <= QUOTE_MAX):
            return None, f"evidence quote length {len(q)} outside {QUOTE_MIN}-{QUOTE_MAX}"
        if q.casefold() not in source_text[nid].casefold():
            return None, f"evidence quote not found verbatim in node {nid}: {q[:60]!r}"
        evidence.append({"node_id": nid, "quote": q})
    declared = set()
    for x in obj.get("source_node_ids") or []:
        try:
            if int(x) in source_text:
                declared.add(int(x))
        except (TypeError, ValueError):
            pass
    nodes = sorted(declared | {e["node_id"] for e in evidence})    # evidence is the authority
    return {"id": "tc-" + uuid.uuid4().hex[:10], "title": title, "type": ctype, "priority": prio,
            "preconditions": _as_list(obj.get("preconditions")), "steps": steps,
            "expected_result": expected, "source_node_ids": nodes, "evidence": evidence}, None


def parse_response(raw: str, sources: list[dict], truncated_by_provider: bool = False) -> Outcome:
    """sources: [{node_id, heading, text}] as given to the LLM."""
    out = Outcome(truncated=truncated_by_provider)
    index = {int(s["node_id"]): normalize(s["heading"] + " " + s["text"]) for s in sources}
    try:
        items, notes, trunc = extract_cases(raw)
    except ValueError as e:
        out.fatal = str(e)
        return out
    out.notes += notes
    out.truncated = out.truncated or trunc
    seen_titles: set[str] = set()
    for i, obj in enumerate(items, start=1):
        case, reason = validate_case(obj, i, index)
        if case is None:
            out.rejected.append({"index": i, "reason": reason,
                                 "raw": obj if isinstance(obj, (dict, list, str, int, float)) else str(obj)})
            continue
        key = case["title"].casefold()
        if key in seen_titles:
            out.rejected.append({"index": i, "reason": "duplicate title", "raw": obj})
            continue
        seen_titles.add(key)
        out.cases.append(case)
    if len(out.cases) > MAX_CASES:
        out.notes.append(f"trimmed_{len(out.cases)}_to_{MAX_CASES}")
        out.cases = out.cases[:MAX_CASES]
    return out


def classify_outcome(o: Outcome) -> str:
    """succeeded | partial | unusable"""
    n = len(o.cases)
    if n < MIN_CASES:
        return "unusable"
    return "succeeded" if not (o.rejected or o.truncated or o.fatal) else "partial"