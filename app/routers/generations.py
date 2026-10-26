from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_session, get_store
from app.services import generation
from app.services.errors import NotFound

router = APIRouter(prefix="/generations", tags=["generations"])


@router.get("/{generation_id}", summary="One generation (with staleness evaluated now)")
def get(generation_id: str, include_raw: bool = False, s: Session = Depends(get_session), store=Depends(get_store)):
    g = generation.get_generation(s, store, generation_id, include_raw)
    if g is None:
        raise NotFound(f"generation {generation_id} not found")
    return g