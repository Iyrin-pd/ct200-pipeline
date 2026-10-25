"""Ingest a PDF as a NEW VERSION of a document. Everything happens in one transaction: a parse
problem or crash leaves the database exactly as it was (no half-built versions)."""
from __future__ import annotations
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.diffing import diff_summary
from app.matching import MatchResult, classify, match_documents
from app.models import Document, DocumentVersion, LogicalNode, Node
from app.parser.model import ParsedDocument, ParsedNode
from app.parser.pdf_parser import parse_pdf
from app.services.errors import Unprocessable
from app.textnorm import normalize, normalize_heading


def latest_version(session: Session, document_id: int) -> DocumentVersion | None:
    return session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                          .order_by(DocumentVersion.version_no.desc()).limit(1))


def load_parsed(session: Session, version_id: int) -> tuple[ParsedDocument, dict[str, Node]]:
    """Rebuild just enough of a ParsedDocument from DB rows for the matcher."""
    rows = session.scalars(select(Node).where(Node.version_id == version_id).order_by(Node.position)).all()
    by_id: dict[int, ParsedNode] = {}
    row_by_key: dict[str, Node] = {}
    nodes: list[ParsedNode] = []
    for r in rows:
        pn = ParsedNode(key=str(r.id), heading=r.heading, number=r.number, level=r.level,
                        numbering_depth=r.numbering_depth, body_text=r.body_text, content_hash=r.content_hash)
        pn.subtree_hash = r.subtree_hash
        by_id[r.id] = pn
        row_by_key[pn.key] = r
        nodes.append(pn)
        if r.parent_id is not None:
            pn.parent = by_id[r.parent_id]
            pn.parent.children.append(pn)
    return ParsedDocument(title=nodes[0].heading, root=nodes[0], nodes=nodes), row_by_key


def _version_summary(match: MatchResult, new_rows: dict[str, Node]) -> dict:
    modified, added, renumbered, unchanged = [], [], [], 0
    for key, m in match.matches.items():
        row = new_rows[key]
        if row.parent_id is None:
            continue
        if m.old.content_hash == m.new.content_hash:
            unchanged += 1
        else:
            d = diff_summary(m.old.heading, m.old.body_text, m.new.heading, m.new.body_text)
            modified.append({"node_id": row.id, "number": row.number, "heading": row.heading,
                             "change_class": d["change_class"], "reasons": d["reasons"]})
        if m.old.number != m.new.number or m.method in ("moved", "heading(renumbered)"):
            renumbered.append({"node_id": row.id, "old_number": m.old.number, "new_number": row.number,
                               "method": m.method})
    for n in match.added:
        added.append({"node_id": new_rows[n.key].id, "number": n.number, "heading": n.heading})
    removed = [{"number": n.number, "heading": n.heading, "old_node_id": int(n.key)} for n in match.removed]
    weak = [{"node_id": new_rows[k].id, "number": new_rows[k].number, "method": m.method, "score": m.score}
            for k, m in match.matches.items() if m.method not in ("root", "number+heading")]
    return {"unchanged": unchanged, "modified": modified, "added": added, "removed": removed,
            "renumbered_or_moved": renumbered, "low_confidence_matches": weak}


def ingest_pdf(session: Session, path: str, slug: str, source_filename: str | None = None,
               strict: bool = True) -> tuple[DocumentVersion, bool]:
    """Returns (version, created). created=False when the identical file is already the latest version."""
    try:
        parsed = parse_pdf(path)
    except Exception as e:                                        # corrupt / not a PDF
        raise Unprocessable(f"could not parse PDF: {type(e).__name__}: {e}")
    problems = [w for w in parsed.warnings if w.code in config.INTEGRITY_WARNINGS]
    problems += [w for n in parsed.nodes for w in n.warnings if w.code in config.INTEGRITY_WARNINGS]
    if strict and problems:
        raise Unprocessable("document structure could not be verified; refusing to ingest (use strict=false to override)",
                            warnings=[w.to_dict() for w in problems][:20])

    doc = session.scalar(select(Document).where(Document.slug == slug))
    if doc is None:
        doc = Document(slug=slug, title=parsed.title)
        session.add(doc)
        session.flush()
    prev = latest_version(session, doc.id)
    if prev is not None and prev.file_sha256 == parsed.source_sha256:
        return prev, False

    ver = DocumentVersion(document_id=doc.id, version_no=(prev.version_no if prev else 0) + 1,
                          source_filename=source_filename or os.path.basename(path),
                          file_sha256=parsed.source_sha256, page_count=parsed.page_count,
                          node_count=len(parsed.nodes), parse_warnings=[w.to_dict() for w in parsed.warnings])
    session.add(ver)
    session.flush()

    match: MatchResult | None = None
    old_rows: dict[str, Node] = {}
    if prev is not None:
        old_parsed, old_rows = load_parsed(session, prev.id)
        match = match_documents(old_parsed, parsed)

    rows: dict[str, Node] = {}
    for pos, pn in enumerate(parsed.nodes):
        m = match.matches.get(pn.key) if match else None
        if m is not None:
            old_row = old_rows[m.old.key]
            logical_id, prev_id = old_row.logical_id, old_row.id
        else:
            ln = LogicalNode(document_id=doc.id, created_in_version=ver.version_no)
            session.add(ln)
            session.flush()
            logical_id, prev_id = ln.id, None
        row = Node(version_id=ver.id, logical_id=logical_id, prev_node_id=prev_id, position=pos,
                   parent_id=rows[pn.parent.key].id if pn.parent else None,
                   sibling_index=pn.parent.children.index(pn) if pn.parent else 0,
                   number=pn.number, heading=pn.heading, level=pn.level, numbering_depth=pn.numbering_depth,
                   path=pn.path, body_text=pn.body_text,
                   search_text=normalize_heading(pn.heading) + " " + normalize(pn.body_text).casefold(),
                   blocks=[b.to_dict() for b in pn.blocks], content_hash=pn.content_hash,
                   subtree_hash=pn.subtree_hash, page_start=pn.page_start, page_end=pn.page_end,
                   warnings=[w.to_dict() for w in pn.warnings], cross_refs=pn.cross_refs,
                   change_status="baseline" if match is None else classify(m),
                   match_method=m.method if m else None, match_score=m.score if m else None)
        session.add(row)
        session.flush()
        rows[pn.key] = row
    ver.summary = _version_summary(match, rows) if match is not None else {"baseline": True}
    session.commit()
    return ver, True