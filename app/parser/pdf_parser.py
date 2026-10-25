"""Layout-aware parser for the CT-200 manual.

A heading must satisfy BOTH: (a) the whole line is bold, and (b) it begins with a section number.
Font size is only a cross-check (produces warnings).
Hierarchy comes from the NUMBERING PREFIX (3.3 -> parent 3), never from physical order or font
size. Every irregularity is recorded as a warning instead of being silently repaired.
"""
from __future__ import annotations
import hashlib
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Optional

import pdfplumber

from app.parser.model import Block, ParsedDocument, ParsedNode, Warning_, compute_subtree_hashes
from app.textnorm import normalize

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
LIST_MARK_RE = re.compile(r"^(?:(\d+|[A-Za-z])[.)]|[•\u2022\-\u2013*])\s+")
XREF_RE = re.compile(r"\b(?:see|Section|Sections)\s+(\d+(?:\.\d+)*(?:\s*(?:,|and)\s*\d+(?:\.\d+)*)*)")
NUM_RE = re.compile(r"\d+(?:\.\d+)*")
TITLE_MIN_SIZE = 18.0


@dataclass
class Event:
    kind: str                       # line | table
    page: int                       # 1-based
    top: float
    x0: float = 0.0
    height: float = 12.0
    text: str = ""
    size: float = 0.0
    bold: bool = False
    block: Optional[Block] = None   # for tables


# ----------------------------------------------------------------------------- extraction
def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def outermost_tables(tables) -> list:
    """pdfplumber reports each wrapped table CELL as its own phantom 'table'.
    Keep only tables not contained inside a larger one."""
    keep = []
    for t in tables:
        b = t.bbox
        contained = any(
            o is not t and _area(o.bbox) > _area(b)
            and o.bbox[0] <= b[0] + 1 and o.bbox[1] <= b[1] + 1
            and o.bbox[2] >= b[2] - 1 and o.bbox[3] >= b[3] - 1
            for o in tables
        )
        if not contained and not any(k.bbox == b for k in keep):
            keep.append(t)
    return keep


def _clean_cell(c) -> str:
    return re.sub(r"\s+", " ", c or "").strip()


def _in_bbox(obj, bboxes) -> bool:
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in bboxes)


def extract_events(pdf, doc_warnings: list[Warning_]) -> list[Event]:
    events: list[Event] = []
    for pno, page in enumerate(pdf.pages, start=1):
        tables = outermost_tables(page.find_tables())
        bboxes = [t.bbox for t in tables]
        for t in tables:
            raw = t.extract()
            rows = [[_clean_cell(c) for c in r] for r in raw if any((c or "").strip() for c in r)]
            if not rows:
                continue
            first = t.rows[0].bbox if t.rows else None
            header_bold = False
            if first:
                gl = [c for c in page.chars if c["text"].strip() and _in_bbox(c, [first])]
                header_bold = bool(gl) and all("Bold" in c["fontname"] for c in gl)
            ncols = {len(r) for r in rows}
            blk = Block(type="table", page=pno)
            if header_bold:
                blk.header, blk.rows = rows[0], rows[1:]
            else:
                blk.rows = rows
                doc_warnings.append(Warning_("table_no_header", f"Table on page {pno} has no bold header row; "
                                             "treated as headerless", "warning"))
            if len(ncols) != 1:
                doc_warnings.append(Warning_("table_ragged", f"Table on page {pno} has ragged rows "
                                             f"{sorted(ncols)}; cells kept as extracted", "warning"))
            events.append(Event("table", pno, t.bbox[1], t.bbox[0], block=blk))
        fpage = page.filter(lambda o, bb=bboxes: o.get("object_type") != "char" or not _in_bbox(o, bb)) if bboxes else page
        for ln in fpage.extract_text_lines(return_chars=True):
            chars = [c for c in ln["chars"] if c["text"].strip()]
            if not chars:
                continue
            bold_ratio = sum("Bold" in c["fontname"] for c in chars) / len(chars)
            size = Counter(round(c["size"], 1) for c in chars).most_common(1)[0][0]
            events.append(Event("line", pno, ln["top"], ln["x0"], ln["bottom"] - ln["top"],
                                ln["text"].strip(), size, bold_ratio >= 0.9))
    events.sort(key=lambda e: (e.page, e.top))
    return events


# ----------------------------------------------------------------------------- entry point
def parse_pdf(path: str) -> ParsedDocument:
    doc_warnings: list[Warning_] = []
    with open(path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    with pdfplumber.open(path) as pdf:
        events = extract_events(pdf, doc_warnings)
        page_count = len(pdf.pages)
    doc = build_tree(events, doc_warnings)
    doc.source_sha256, doc.page_count = sha, page_count
    lost, extra = coverage_diff(path, doc)
    if lost:
        doc.warnings.append(Warning_("content_loss", f"{sum(lost.values())} source word(s) not found in parsed tree: "
                                     f"{dict(list(lost.items())[:15])}"))
    if extra:
        doc.warnings.append(Warning_("content_added", f"{sum(extra.values())} parsed word(s) not present in source: "
                                     f"{dict(list(extra.items())[:15])}"))
    return doc


def _tok(text: str) -> Counter:
    return Counter(t.rstrip(".") for t in normalize(text).split() if t.rstrip("."))


def parsed_words(doc: ParsedDocument) -> Counter:
    c: Counter = Counter()
    c += _tok(doc.title)
    for n in doc.nodes[1:]:
        c += _tok((n.number or "") + " " + n.heading)
        for b in n.blocks:
            if b.type == "paragraph":
                c += _tok(b.text)
            elif b.type == "list":
                c += _tok(" ".join(b.items))
            else:
                c += _tok(" ".join(b.header) + " " + " ".join(" ".join(r) for r in b.rows))
    return c


def coverage_diff(path: str, doc: ParsedDocument) -> tuple[Counter, Counter]:
    """Bag-of-words comparison PDF vs parsed tree. Anything dropped or invented shows up here,
    so structural bugs can't hide behind a clean-looking hierarchy."""
    src: Counter = Counter()
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            src += _tok(" ".join(w["text"] for w in page.extract_words()))
    got = parsed_words(doc)
    return src - got, got - src


# ----------------------------------------------------------------------------- structure
def build_tree(events: list[Event], doc_warnings: list[Warning_]) -> ParsedDocument:
    body_lines = [e for e in events if e.kind == "line" and not e.bold and e.size < TITLE_MIN_SIZE]
    margin = statistics.mode([round(e.x0) for e in body_lines]) if body_lines else 0
    body_size = statistics.mode([e.size for e in body_lines]) if body_lines else 11.0

    root = ParsedNode(key="n0", heading="", level=0)
    nodes: list[ParsedNode] = [root]
    stack: list[ParsedNode] = [root]
    title_parts: list[str] = []
    current = root
    seen_heading = False

    para: list[str] = []
    para_last: Optional[Event] = None
    lst: Optional[Block] = None
    lst_x = 0.0

    def flush_para():
        nonlocal para, para_last
        if para:
            current.blocks.append(Block("paragraph", text=" ".join(para), page=para_last.page))
        para, para_last = [], None

    def flush_list():
        nonlocal lst
        if lst and lst.items:
            current.blocks.append(lst)
        lst = None

    def flush():
        flush_para()
        flush_list()

    for ev in events:
        if ev.kind == "table":
            flush()
            current.blocks.append(ev.block)
            current.page_end = max(current.page_end, ev.page)
            para_last = None
            continue

        # ---- title (page 1, big bold text before any numbered heading)
        if not seen_heading and ev.size >= TITLE_MIN_SIZE and ev.bold:
            title_parts.append(ev.text)
            continue

        m = HEADING_RE.match(ev.text) if ev.bold else None
        if m:
            flush()
            seen_heading = True
            number, heading = m.group(1), m.group(2).strip()
            tokens = tuple(int(x) for x in number.split("."))
            # parent = deepest open node whose number is a PROPER PREFIX of this one
            while len(stack) > 1 and not (len(stack[-1].tokens) < len(tokens)
                                          and tokens[:len(stack[-1].tokens)] == stack[-1].tokens):
                stack.pop()
            parent = stack[-1]
            node = ParsedNode(key=f"n{len(nodes)}", heading=heading, number=number, parent=parent,
                              level=parent.level + 1, numbering_depth=len(tokens),
                              page_start=ev.page, page_end=ev.page, font_size=ev.size)
            expected_depth = len(parent.tokens) + 1
            if parent is root and len(tokens) > 1:
                node.warn("orphan_numbering", f"{number} has no ancestor heading in the document; attached to the title root")
            elif len(tokens) > expected_depth:
                node.warn("skipped_levels",
                          f"Numbering depth {len(tokens)} jumps from parent {parent.number} (depth {len(parent.tokens)}); "
                          f"intermediate heading(s) missing. Attached to {parent.number} at tree level {node.level}.")
            sibs = parent.children
            if sibs and sibs[-1].tokens and sibs[-1].tokens[-1] > tokens[-1] and len(sibs[-1].tokens) == len(tokens):
                node.warn("out_of_order", f"{number} appears after {sibs[-1].number} in the PDF; document order preserved, "
                                          f"parent resolved from numbering")
            if any(n.number == number for n in nodes):
                node.warn("duplicate_number", f"Section number {number} occurs more than once")
            parent.children.append(node)
            nodes.append(node)
            stack.append(node)
            current = node
            continue

        # ---- body content
        if ev.bold and not m:
            current.warn("unnumbered_bold_line", f"Bold line without section number kept as body text, not a heading: {ev.text!r}")
        current.page_end = max(current.page_end, ev.page)
        marker = LIST_MARK_RE.match(ev.text)
        in_list = lst is not None
        if marker and not ev.bold and (ev.x0 > margin + 8 or in_list):
            flush_para()
            if lst is None:
                lst = Block("list", ordered=bool(marker.group(1)), page=ev.page)
            lst.items.append(ev.text)
            lst_x = ev.x0
            continue
        if in_list and ev.x0 > lst_x + 3:           # wrapped continuation of previous item
            lst.items[-1] += " " + ev.text
            continue
        flush_list()
        if para and para_last is not None and para_last.page == ev.page and (ev.top - para_last.top) > 1.6 * max(ev.height, 8):
            flush_para()
        para.append(ev.text)                         # page changes: same paragraph, joined with a space
        para_last = ev
    flush()

    root.heading = " ".join(title_parts).strip()
    if not root.heading:
        root.heading = "(untitled document)"
        doc_warnings.append(Warning_("missing_title", "No title block detected on page 1"))

    _finalize(root, nodes, body_size, doc_warnings)
    return ParsedDocument(title=root.heading, root=root, nodes=nodes, warnings=doc_warnings)


def _finalize(root: ParsedNode, nodes: list[ParsedNode], body_size: float, doc_warnings: list[Warning_]) -> None:
    modal: dict[int, float] = {}
    for d in (1, 2):
        sizes = [n.font_size for n in nodes if n.numbering_depth == d and n.font_size]
        if sizes:
            modal[d] = Counter(sizes).most_common(1)[0][0]
    for n in nodes[1:]:
        if n.font_size is not None and n.font_size <= body_size + 0.05:
            n.warn("heading_style_body_sized", "Heading is bold but body-sized; accepted because it carries a section number", "info")
        elif n.numbering_depth in modal and n.font_size != modal[n.numbering_depth]:
            n.warn("heading_style_mismatch", f"Font size {n.font_size} differs from other depth-{n.numbering_depth} headings ({modal[n.numbering_depth]})", "info")
    by_head: dict[str, list[ParsedNode]] = {}
    for n in nodes[1:]:
        by_head.setdefault(n.heading.casefold(), []).append(n)
    for group in by_head.values():
        if len(group) > 1:
            for n in group:
                others = ", ".join(o.number for o in group if o is not n)
                n.warn("duplicate_heading", f"Heading text also used by section(s) {others}; identity relies on number + parent, not text", "info")

    def walk(n: ParsedNode, trail: list[str]):
        trail2 = trail + ([n.number] if n.number else [])
        n.path = " > ".join(trail2)
        n.finalize()
        n.cross_refs = sorted({x for m in XREF_RE.finditer(n.body_text) for x in NUM_RE.findall(m.group(1))})
        for c in n.children:
            walk(c, trail2)
    walk(root, [])
    for n in nodes:
        if not n.blocks and not n.children and n is not root:
            n.warn("empty_section", "Heading has no body text and no children")
    compute_subtree_hashes(root)