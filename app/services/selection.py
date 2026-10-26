"""Selections: a named set of (node, version) pins.

Pinning works because a Node row belongs to exactly ONE document version and is never updated
after ingestion. Re-ingesting creates new rows; old selections keep pointing at old rows, so
`selection -> text` is reproducible forever.

Policies:
  * A selection must come from ONE version - mixing v1 and v2 excerpts would show the model (and
    the reader) contradictory facts as if they were one document.
  * Identity = the set of pinned nodes. Re-submitting the same set (any name) returns the
    existing selection (HTTP 200, deduplicated=true); the requested name is NOT applied.
  * Same name, different set -> a new selection. Names are labels, not keys.
"""
from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import DocumentVersion, Node, Selection, SelectionItem
from app.services.errors import NotFound, Unprocessable
from app.services.queries import latest_nodes_by_logical
from app.textnorm import sha256


def _descendants(session: Session, ids: set[int]) -> set[int]:
    out, frontier = set(ids), set(ids)
    while frontier:
        kids = set(session.scalars(select(Node.id).where(Node.parent_id.in_(list(frontier)))))
        frontier = kids - out
        out |= kids
    return out


def create_selection(session: Session, name: str, node_ids: list[int],
                     include_descendants: bool = False) -> tuple[Selection, bool, bool]:
    """-> (selection, created, requested_name_ignored)"""
    name = (name or "").strip()
    if not name:
        raise Unprocessable("selection name must not be empty")
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        raise Unprocessable("node_ids must contain at least one node id")
    nodes = {n.id: n for n in session.scalars(select(Node).where(Node.id.in_(ids)))}
    missing = [i for i in ids if i not in nodes]
    if missing:
        raise NotFound(f"node id(s) not found: {missing}")
    if len({n.version_id for n in nodes.values()}) > 1:
        raise Unprocessable("all nodes in a selection must belong to the same document version",
                            versions={i: n.version_id for i, n in nodes.items()})
    if any(n.parent_id is None for n in nodes.values()):
        raise Unprocessable("the document title (root) cannot be selected; select one or more sections")
    if include_descendants:
        ids = sorted(_descendants(session, set(ids)))
        nodes = {n.id: n for n in session.scalars(select(Node).where(Node.id.in_(ids)))}
    if len(nodes) > config.MAX_SELECTION_NODES:
        raise Unprocessable(f"selection has {len(nodes)} nodes; maximum is {config.MAX_SELECTION_NODES}")
    ordered = sorted(nodes.values(), key=lambda n: n.position)
    fp = sha256("|".join(f"{n.id}:{n.content_hash}" for n in ordered))
    existing = session.scalar(select(Selection).where(Selection.fingerprint == fp))
    if existing is not None:
        return existing, False, existing.name != name

    ver = session.get(DocumentVersion, ordered[0].version_id)
    sel = Selection(name=name, document_id=ver.document_id, version_id=ver.id, fingerprint=fp)
    sel.items = [SelectionItem(node_id=n.id, logical_id=n.logical_id, version_no=ver.version_no,
                               content_hash=n.content_hash, position=i) for i, n in enumerate(ordered)]
    session.add(sel)
    session.commit()
    return sel, True, False


def get_selection(session: Session, selection_id: int) -> Selection:
    s = session.get(Selection, selection_id)
    if s is None:
        raise NotFound(f"selection {selection_id} not found")
    return s


def sources_for(session: Session, sel: Selection) -> list[dict]:
    """Exact pinned text (what the selection was created against), in document order."""
    nodes = {n.id: n for n in session.scalars(select(Node).where(Node.id.in_([i.node_id for i in sel.items])))}
    out = []
    for it in sel.items:
        n = nodes[it.node_id]
        out.append({"node_id": n.id, "logical_id": n.logical_id, "version_no": it.version_no, "number": n.number,
                    "heading": n.heading, "path": n.path, "text": n.body_text, "content_hash": n.content_hash})
    return out


def selection_view(session: Session, sel: Selection) -> dict:
    srcs = sources_for(session, sel)
    cur = latest_nodes_by_logical(session, sel.document_id, [s["logical_id"] for s in srcs])
    items = []
    for s in srcs:
        c = cur.get(s["logical_id"])
        state = "removed_in_latest" if c is None else ("unchanged_in_latest" if c["content_hash"] == s["content_hash"]
                                                       else "changed_in_latest")
        items.append({**s, "latest_state": state, "latest_node_id": c["node_id"] if c else None,
                      "latest_version_no": c["version_no"] if c else None})
    return {"id": sel.id, "name": sel.name, "document_id": sel.document_id, "version_id": sel.version_id,
            "version_no": srcs[0]["version_no"] if srcs else None, "created_at": sel.created_at.isoformat(),
            "fingerprint": sel.fingerprint, "items": items,
            "pinned_text_is_immutable": True}