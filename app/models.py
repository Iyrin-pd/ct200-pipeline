from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document", order_by="DocumentVersion.version_no")


class DocumentVersion(Base):
    """One ingestion of one PDF. Immutable after creation: v1 is never touched by v2."""
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    source_filename: Mapped[str] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_warnings: Mapped[list] = mapped_column(JSON, default=list)       # document-level
    summary: Mapped[dict] = mapped_column(JSON, default=dict)              # diff vs previous version
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[Document] = relationship(back_populates="versions")


class LogicalNode(Base):
    """Identity of a section ACROSS versions. Node rows are per-version; this ties them together."""
    __tablename__ = "logical_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    created_in_version: Mapped[int] = mapped_column(Integer)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (Index("ix_nodes_version_parent", "version_id", "parent_id"),
                      Index("ix_nodes_logical_version", "logical_id", "version_id"))
    id: Mapped[int] = mapped_column(primary_key=True)                      # the public node id
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    logical_id: Mapped[int] = mapped_column(ForeignKey("logical_nodes.id"))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    prev_node_id: Mapped[Optional[int]] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    position: Mapped[int] = mapped_column(Integer)                         # document order within the version
    sibling_index: Mapped[int] = mapped_column(Integer, default=0)
    number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    heading: Mapped[str] = mapped_column(String(500))
    level: Mapped[int] = mapped_column(Integer)                            # tree depth, root = 0
    numbering_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(String(200), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    search_text: Mapped[str] = mapped_column(Text, default="")             # normalised + casefolded
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    subtree_hash: Mapped[str] = mapped_column(String(64))
    page_start: Mapped[int] = mapped_column(Integer, default=1)
    page_end: Mapped[int] = mapped_column(Integer, default=1)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    cross_refs: Mapped[list] = mapped_column(JSON, default=list)
    change_status: Mapped[str] = mapped_column(String(20), default="baseline")   # baseline|unchanged|modified|added
    match_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[list["SelectionItem"]] = relationship(order_by="SelectionItem.position", cascade="all, delete-orphan")


class SelectionItem(Base):
    """A (node, version) pin. node_id belongs to exactly one version, so the pair is implicit."""
    __tablename__ = "selection_items"
    __table_args__ = (UniqueConstraint("selection_id", "node_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    selection_id: Mapped[int] = mapped_column(ForeignKey("selections.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"))
    logical_id: Mapped[int] = mapped_column(Integer)
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))                   # hash at pin time (audit)
    position: Mapped[int] = mapped_column(Integer)