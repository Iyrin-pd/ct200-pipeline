from __future__ import annotations
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class IngestPathRequest(BaseModel):
    path: str = Field(..., description="PDF path relative to the server's data directory, e.g. 'ct200_manual.pdf'")
    strict: bool = Field(True, description="Refuse to ingest if the parser cannot verify structural integrity")


class SelectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    node_ids: list[int] = Field(..., min_length=1, max_length=200)
    include_descendants: bool = False


class GenerateRequest(BaseModel):
    force: bool = Field(False, description="Create a new generation even if an equivalent one exists")


class NodeSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    logical_id: int
    number: Optional[str] = None
    heading: str
    level: int
    parent_id: Optional[int] = None
    content_hash: str
    change_status: str
    version_no: Optional[int] = None


class NodeDetail(NodeSummary):
    body_text: str
    blocks: list[dict[str, Any]]
    subtree_hash: str
    children: list[NodeSummary]
    breadcrumbs: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


class VersionOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    version_no: int
    source_filename: str
    file_sha256: str
    page_count: int
    node_count: int
    parse_warnings: list[dict[str, Any]]
    summary: dict[str, Any]