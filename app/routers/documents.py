import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.deps import get_session
from app.models import Document, DocumentVersion, Node
from app.schemas import IngestPathRequest, VersionOut
from app.services import queries
from app.services.errors import NotFound, Unprocessable
from app.services.ingest import ingest_pdf

router = APIRouter(prefix="/documents", tags=["documents"])


def _ver_out(v: DocumentVersion) -> dict:
    return {"id": v.id, "document_id": v.document_id, "version_no": v.version_no, "source_filename": v.source_filename,
            "file_sha256": v.file_sha256, "page_count": v.page_count, "node_count": v.node_count,
            "created_at": v.created_at.isoformat(), "parse_warnings": v.parse_warnings, "summary": v.summary}


@router.post("/{slug}/versions", response_model=VersionOut, status_code=201,
             summary="Ingest a PDF (from the server's data dir) as a new version")
def ingest_from_path(slug: str, body: IngestPathRequest, response: Response, s: Session = Depends(get_session)):
    target = (config.DATA_DIR / body.path).resolve()
    if config.DATA_DIR not in target.parents:
        raise Unprocessable("path must be inside the server data directory")
    if target.suffix.lower() != ".pdf" or not target.is_file():
        raise NotFound(f"no such PDF in data directory: {body.path}")
    ver, created = ingest_pdf(s, str(target), slug, source_filename=target.name, strict=body.strict)
    if not created:
        response.status_code = 200          # identical file already is the latest version: idempotent
    return _ver_out(ver) | {"created": created}


@router.post("/{slug}/versions/upload", response_model=VersionOut, status_code=201,
             summary="Ingest an uploaded PDF as a new version")
def ingest_upload(slug: str, response: Response, file: UploadFile = File(...), strict: bool = True,
                  s: Session = Depends(get_session)):
    import os
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as tmp:
            shutil.copyfileobj(file.file, tmp)
        ver, created = ingest_pdf(s, tmp_path, slug, source_filename=file.filename, strict=strict)
    finally:
        os.remove(tmp_path)
    if not created:
        response.status_code = 200
    return _ver_out(ver) | {"created": created}


@router.get("", summary="List documents")
def list_documents(s: Session = Depends(get_session)):
    out = []
    for d in s.scalars(select(Document).order_by(Document.id)):
        lv = max((v.version_no for v in d.versions), default=None)
        out.append({"id": d.id, "slug": d.slug, "title": d.title, "latest_version": lv, "versions": len(d.versions)})
    return out


@router.get("/{slug}/versions", summary="List versions of a document")
def list_versions(slug: str, s: Session = Depends(get_session)):
    doc = queries.get_document(s, slug)
    return [_ver_out(v) for v in doc.versions]


@router.get("/{slug}/versions/{version_no}", response_model=VersionOut, summary="Version detail incl. diff summary vs previous")
def version_detail(slug: str, version_no: int, s: Session = Depends(get_session)):
    doc = queries.get_document(s, slug)
    return _ver_out(queries.resolve_version(s, doc, version_no))


@router.get("/{slug}/sections", summary="Top-level sections (default: latest version)")
def sections(slug: str, version: str = Query("latest"), s: Session = Depends(get_session)):
    doc = queries.get_document(s, slug)
    ver = queries.resolve_version(s, doc, version)
    return {"document": slug, "title": doc.title, "version_no": ver.version_no,
            "sections": queries.top_level_sections(s, doc, ver)}


@router.get("/{slug}/tree", summary="Whole tree, nested (default: latest version)")
def tree(slug: str, version: str = Query("latest"), s: Session = Depends(get_session)):
    doc = queries.get_document(s, slug)
    ver = queries.resolve_version(s, doc, version)
    rows = s.scalars(select(Node).where(Node.version_id == ver.id).order_by(Node.position)).all()
    by_id = {r.id: queries.node_summary(r, ver.version_no) | {"children": []} for r in rows}
    root = None
    for r in rows:
        if r.parent_id is None:
            root = by_id[r.id]
        else:
            by_id[r.parent_id]["children"].append(by_id[r.id])
    return {"document": slug, "version_no": ver.version_no, "tree": root}


@router.get("/{slug}/search", summary="Search headings and/or text")
def search(slug: str, q: str, scope: str = Query("all", pattern="^(all|heading|text)$"),
           version: str = Query("latest"), level: Optional[int] = None, limit: int = Query(50, ge=1, le=200),
           s: Session = Depends(get_session)):
    doc = queries.get_document(s, slug)
    ver = queries.resolve_version(s, doc, version)
    hits = queries.search(s, doc, ver, q, scope, level, limit)
    return {"query": q, "scope": scope, "version_no": ver.version_no, "count": len(hits), "results": hits}