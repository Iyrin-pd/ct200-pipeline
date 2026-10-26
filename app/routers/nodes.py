from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_session, get_store
from app.schemas import NodeDetail
from app.services import generation, queries

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("/{node_id}", response_model=NodeDetail, summary="Node with children, full text and content hash")
def get_node(node_id: int, s: Session = Depends(get_session)):
    return queries.node_detail(s, queries.get_node(s, node_id))


@router.get("/{node_id}/changes", summary="Has this node changed across versions? (+ lightweight diff)")
def node_changes(node_id: int, compare_to: Optional[int] = Query(None, description="version_no to compare with; default latest"),
                 s: Session = Depends(get_session)):
    return queries.node_changes(s, queries.get_node(s, node_id), compare_to)


@router.get("/{node_id}/test-cases", summary="Generated test cases that cite this section (any version), with staleness")
def node_test_cases(node_id: int, include_raw: bool = False, s: Session = Depends(get_session), store=Depends(get_store)):
    return generation.for_node(s, store, node_id, include_raw)