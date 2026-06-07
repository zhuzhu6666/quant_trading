"""GET /api/shadow + POST /api/shadow/{promote,demote}."""
from fastapi import APIRouter
from pydantic import BaseModel

from backend.services.shadow_service import demote, list_shadows, promote

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


class NameRequest(BaseModel):
    name: str


@router.get("")
def list_() -> dict:
    return {"shadows": list_shadows()}


@router.post("/promote")
def promote_factor(req: NameRequest) -> dict:
    return promote(req.name)


@router.post("/demote")
def demote_factor(req: NameRequest) -> dict:
    return demote(req.name)
