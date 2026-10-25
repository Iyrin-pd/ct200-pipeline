"""Tiny builders for synthetic ParsedDocuments (matcher/edge-case tests without PDFs)."""
from app.parser.model import Block, ParsedDocument, ParsedNode, compute_subtree_hashes


def build(spec, title="T") -> ParsedDocument:
    """spec: list of (number, heading, body, [children...])"""
    root = ParsedNode(key="n0", heading=title, level=0)
    nodes = [root]

    def add(items, parent):
        for number, heading, body, kids in items:
            n = ParsedNode(key=f"n{len(nodes)}", heading=heading, number=number, parent=parent,
                           level=parent.level + 1, numbering_depth=len(number.split(".")))
            if body:
                n.blocks = [Block("paragraph", text=body)]
            parent.children.append(n)
            nodes.append(n)
            add(kids, n)
    add(spec, root)
    for n in nodes:
        n.finalize()
    compute_subtree_hashes(root)
    return ParsedDocument(title=title, root=root, nodes=nodes)


def find(doc, heading, number=None):
    r = [n for n in doc.nodes if n.heading == heading and (number is None or n.number == number)]
    assert len(r) == 1, (heading, number, len(r))
    return r[0]