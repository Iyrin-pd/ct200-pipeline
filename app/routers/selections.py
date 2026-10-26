from fastapi import APIRouter, Depends, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_llm, get_session, get_store
from app.models import Selection
from app.schemas import GenerateRequest, SelectionCreate
from app.services import generation, selection as sel_svc

router = APIRouter(prefix="/selections", tags=["selections"])


@router.post("", status_code=201, summary="Create a version-pinned selection (idempotent on the set of nodes)")
def create(body: SelectionCreate, response: Response, s: Session = Depends(get_session)):
    sel, created, name_ignored = sel_svc.create_selection(s, body.name, body.node_ids, body.include_descendants)
    if not created:
        response.status_code = 200
    return sel_svc.selection_view(s, sel) | {"created": created, "deduplicated": not created,
                                             "requested_name_ignored": name_ignored}


@router.get("", summary="List selections")
def list_all(s: Session = Depends(get_session)):
    return [{"id": x.id, "name": x.name, "version_id": x.version_id, "items": len(x.items),
             "created_at": x.created_at.isoformat()} for x in s.scalars(select(Selection).order_by(Selection.id))]


@router.get("/{selection_id}", summary="Selection with its pinned (immutable) text")
def get(selection_id: int, s: Session = Depends(get_session)):
    return sel_svc.selection_view(s, sel_svc.get_selection(s, selection_id))


@router.post("/{selection_id}/generate", status_code=201, summary="Generate 3-5 QA test case ideas from the selection")
def generate(selection_id: int, response: Response, body: GenerateRequest = GenerateRequest(),
             s: Session = Depends(get_session), store=Depends(get_store), llm=Depends(get_llm)):
    gen, created = generation.generate(s, store, llm, selection_id, body.force)
    presented = generation.present(s, gen, include_raw=False)
    if gen["status"] == "failed":
        return JSONResponse(status_code=502, content=jsonable_encoder(
            {"error": "llm_failure", "message": gen["error"], "generation": presented}))
    if not created:
        response.status_code = 200
    return presented | {"reused": not created}


@router.get("/{selection_id}/test-cases", summary="All generations for a selection, each case annotated with staleness")
def test_cases(selection_id: int, include_raw: bool = False, s: Session = Depends(get_session), store=Depends(get_store)):
    return generation.for_selection(s, store, selection_id, include_raw)