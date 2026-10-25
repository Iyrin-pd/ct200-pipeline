"""Read-side: browsing, searching, per-node change info, and 'latest node for logical id' lookups."""
from __future__ import annotations
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.diffing import diff_summary
from app.models import Document, DocumentVersion, Node
from app.services.errors import NotFound, Unprocessable
from app.services.ingest import latest_version
from app.textnorm import normalize


def get_document(session: Session, slug: str) -> Document:
    doc = session.scalar(select(Document).where(Document.slug == slug))
    if doc is None:
        raise NotFound(f"document '{slug}' not found")
    return doc


def resolve_version(session: Session, doc: Document, version: str | int | None) -> DocumentVersion:
    if version in (None, "", "latest"):
        v = latest_version(session, doc.id)
    else:
        try:
            n = int(version)
        except (TypeError, ValueError):
            raise Unprocessable(f"version must be an integer or 'latest', got {version!r}")
        v = session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                         DocumentVersion.version_no == n))
    if v is None:
        raise NotFound(f"version {version!r} of '{doc.slug}' not found")
    return v


def get_node(session: Session, node_id: int) -> Node:
    n = session.get(Node, node_id)
    if n is None:
        raise NotFound(f"node {node_id} not found")
    return n


def node_summary(n: Node, version_no: int | None = None) -> dict:
    return {"id": n.id, "logical_id": n.logical_id, "number": n.number, "heading": n.heading, "level": n.level,
            "parent_id": n.parent_id, "position": n.position, "content_hash": n.content_hash,
            "change_status": n.change_status, "version_no": version_no}


def children_of(session: Session, node_id: int) -> list[Node]:
    return list(session.scalars(select(Node).where(Node.parent_id == node_id).order_by(Node.position)))


def node_detail(session: Session, n: Node) -> dict:
    ver = session.get(DocumentVersion, n.version_id)
    d = node_summary(n, ver.version_no)
    parent = session.get(Node, n.parent_id) if n.parent_id else None
    crumbs, p = [], parent
    while p is not None and p.parent_id is not None:
        crumbs.append({"id": p.id, "number": p.number, "heading": p.heading})
        p = session.get(Node, p.parent_id)
    refs = []
    for num in n.cross_refs or []:
        t = session.scalar(select(Node).where(Node.version_id == n.version_id, Node.number == num))
        refs.append({"number": num, "node_id": t.id if t else None, "heading": t.heading if t else None})
    d.update(body_text=n.body_text, blocks=n.blocks, subtree_hash=n.subtree_hash, path=n.path,
             numbering_depth=n.numbering_depth, pages=[n.page_start, n.page_end], warnings=n.warnings,
             breadcrumbs=list(reversed(crumbs)), cross_references=refs, document_id=ver.document_id,
             match_method=n.match_method, previous_version_node_id=n.prev_node_id,
             children=[node_summary(c, ver.version_no) for c in children_of(session, n.id)])
    return d


def top_level_sections(session: Session, doc: Document, ver: DocumentVersion) -> list[dict]:
    root = session.scalar(select(Node).where(Node.version_id == ver.id, Node.parent_id.is_(None)))
    return [node_summary(c, ver.version_no) | {"child_count": len(children_of(session, c.id))}
            for c in children_of(session, root.id)]


def search(session: Session, doc: Document, ver: DocumentVersion, q: str, scope: str = "all",
           level: int | None = None, limit: int = 50) -> list[dict]:
    qn = normalize(q).casefold()
    if not qn:
        raise Unprocessable("query 'q' must not be empty")
    stmt = select(Node).where(Node.version_id == ver.id, Node.parent_id.is_not(None))
    head_hit = func.lower(Node.heading).contains(qn, autoescape=True)
    text_hit = Node.search_text.contains(qn, autoescape=True)
    if scope == "heading":
        stmt = stmt.where(head_hit)
    elif scope == "text":
        stmt = stmt.where(text_hit)
    elif scope == "all":
        stmt = stmt.where(or_(text_hit, head_hit))
    else:
        raise Unprocessable("scope must be heading|text|all")
    if level is not None:
        stmt = stmt.where(Node.level == level)
    out = []
    for n in session.scalars(stmt.order_by(Node.position).limit(limit)):
        hay = normalize(n.body_text)
        i = hay.casefold().find(qn)
        snippet = (("…" if i > 60 else "") + hay[max(0, i - 60): i + len(qn) + 60] + "…") if i >= 0 else None
        out.append(node_summary(n, ver.version_no) | {"matched_in": "heading" if qn in n.heading.casefold() else "text",
                                                      "snippet": snippet})
    return out


# ----------------------------------------------------------------------------- change info
def latest_nodes_by_logical(session: Session, document_id: int, logical_ids: list[int]) -> dict[int, dict | None]:
    """logical_id -> latest-version node snapshot (or None if the section vanished)."""
    lv = latest_version(session, document_id)
    out: dict[int, dict | None] = {lid: None for lid in logical_ids}
    if lv is None or not logical_ids:
        return out
    for n in session.scalars(select(Node).where(Node.version_id == lv.id, Node.logical_id.in_(logical_ids))):
        out[n.logical_id] = {"node_id": n.id, "version_no": lv.version_no, "number": n.number, "heading": n.heading,
                             "text": n.body_text, "content_hash": n.content_hash}
    return out


def node_changes(session: Session, node: Node, compare_to: int | None = None) -> dict:
    ver = session.get(DocumentVersion, node.version_id)
    rows = session.execute(select(Node, DocumentVersion.version_no)
                           .join(DocumentVersion, DocumentVersion.id == Node.version_id)
                           .where(Node.logical_id == node.logical_id)
                           .order_by(DocumentVersion.version_no)).all()
    history = [{"version_no": vn, "node_id": n.id, "number": n.number, "heading": n.heading,
                "content_hash": n.content_hash, "change_status": n.change_status, "match_method": n.match_method}
               for n, vn in rows]
    latest_v = latest_version(session, ver.document_id)
    target_version = latest_v.version_no if compare_to is None else compare_to
    target = next((n for n, vn in rows if vn == target_version), None)
    res = {"node_id": node.id, "logical_id": node.logical_id, "this_version": ver.version_no,
           "compared_with_version": target_version, "history": history,
           "change_status_in_this_version": node.change_status}
    if target is None:
        res |= {"status": "removed" if compare_to is None else "absent", "changed": True, "diff": None,
                "subtree_changed": None,
                "note": f"this section has no counterpart in version {target_version}"}
        return res
    if target.id == node.id:
        res |= {"status": "same_version", "changed": False, "diff": None, "subtree_changed": False,
                "note": "this node already belongs to the version being compared"}
        return res
    d = diff_summary(node.heading, node.body_text, target.heading, target.body_text)
    res |= {"status": "modified" if d["changed"] else "unchanged", "changed": d["changed"], "diff": d,
            "subtree_changed": node.subtree_hash != target.subtree_hash,
            "counterpart_node_id": target.id,
            "content_hash_then": node.content_hash, "content_hash_now": target.content_hash}
    return res