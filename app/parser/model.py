"""Plain dataclasses produced by the parser. No DB / framework imports on purpose."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from app.textnorm import content_hash as _content_hash, sha256


@dataclass
class Block:
    type: str                                   # paragraph | list | table
    text: str = ""                              # paragraph
    items: list[str] = field(default_factory=list)       # list
    ordered: bool = False
    header: list[str] = field(default_factory=list)      # table
    rows: list[list[str]] = field(default_factory=list)  # table
    page: int = 0

    def to_text(self) -> str:
        if self.type == "paragraph":
            return self.text
        if self.type == "list":
            return "\n".join(f"- {i}" if not self.ordered else i for i in self.items)
        if self.type == "table":
            out = []
            if self.header:
                out.append("| " + " | ".join(self.header) + " |")
                out.append("|" + " --- |" * len(self.header))
            out += ["| " + " | ".join(r) + " |" for r in self.rows]
            return "\n".join(out)
        return ""

    def to_dict(self) -> dict:
        d = {"type": self.type, "page": self.page}
        if self.type == "paragraph":
            d["text"] = self.text
        elif self.type == "list":
            d.update(items=self.items, ordered=self.ordered)
        elif self.type == "table":
            d.update(header=self.header, rows=self.rows)
        return d


@dataclass
class Warning_:
    code: str
    message: str
    severity: str = "warning"   # info | warning

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class ParsedNode:
    key: str                                    # unique within one parse, doc order: n0 (title/root), n1, ...
    heading: str
    number: Optional[str] = None                # "2.1.1.1"; None for the title root
    level: int = 0                              # depth in the TREE (root=0). Not the numbering depth.
    numbering_depth: Optional[int] = None       # number of dotted components (2.1.1.1 -> 4)
    parent: Optional["ParsedNode"] = None
    children: list["ParsedNode"] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    page_start: int = 1
    page_end: int = 1
    font_size: Optional[float] = None
    warnings: list[Warning_] = field(default_factory=list)
    cross_refs: list[str] = field(default_factory=list)
    # computed in finalize()
    body_text: str = ""
    content_hash: str = ""
    subtree_hash: str = ""
    path: str = ""                              # "3 > 3.3" style breadcrumb of numbers

    @property
    def tokens(self) -> tuple[int, ...]:
        return tuple(int(x) for x in self.number.split(".")) if self.number else ()

    def warn(self, code: str, message: str, severity: str = "warning") -> None:
        self.warnings.append(Warning_(code, message, severity))

    def finalize(self) -> None:
        self.body_text = "\n\n".join(b.to_text() for b in self.blocks if b.to_text())
        self.content_hash = _content_hash(self.heading, self.body_text)


@dataclass
class ParsedDocument:
    title: str
    root: ParsedNode
    nodes: list[ParsedNode]                     # flat, document order, root first
    warnings: list[Warning_] = field(default_factory=list)
    source_sha256: str = ""
    page_count: int = 0

    def by_number(self, number: str) -> list[ParsedNode]:
        return [n for n in self.nodes if n.number == number]


def compute_subtree_hashes(node: ParsedNode) -> str:
    """Merkle-style hash. Child hashes are SORTED so pure reordering of siblings
    (e.g. 3.4 appearing before 3.3) does not flag the parent as changed."""
    child = sorted(compute_subtree_hashes(c) for c in node.children)
    node.subtree_hash = sha256(node.content_hash + "|" + "|".join(child))
    return node.subtree_hash