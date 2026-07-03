"""GET /api/shadow + POST /api/shadow/{promote,demote}."""
from fastapi import APIRouter
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.services.shadow_service import demote, list_shadows, promote
from backend.services.mutation_audit import record_api_mutation

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


class NameRequest(BaseModel):
    name: str


@router.get("")
def list_(_user: RequireUser)-> dict:
    return {"shadows": list_shadows()}


@router.post("/promote")
def promote_factor(_user: RequireUser, req: NameRequest)-> dict:
    result = promote(req.name)
    record_api_mutation(
        user=_user,
        endpoint="/api/shadow/promote",
        action="promote_shadow_factor",
        status="applied" if result.get("ok", True) else "blocked",
        result={"name": req.name, **result},
    )
    return result


@router.post("/demote")
def demote_factor(_user: RequireUser, req: NameRequest)-> dict:
    result = demote(req.name)
    record_api_mutation(
        user=_user,
        endpoint="/api/shadow/demote",
        action="demote_shadow_factor",
        status="applied" if result.get("ok", True) else "blocked",
        result={"name": req.name, **result},
    )
    return result
