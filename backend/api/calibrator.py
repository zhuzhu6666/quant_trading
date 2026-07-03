"""GET/POST /api/calibrator."""
from fastapi import APIRouter, HTTPException
from backend.core.auth import RequireUser
from pydantic import BaseModel

from backend.services.calibrator_service import get_status, load, save
from backend.services.mutation_audit import record_api_mutation

router = APIRouter(prefix="/api/calibrator", tags=["calibrator"])


class SaveRequest(BaseModel):
    buckets: list[dict]


@router.get("")
def read(_user: RequireUser)-> dict:
    return get_status()


@router.post("/save")
def save_buckets(_user: RequireUser, req: SaveRequest)-> dict:
    result = save(req.buckets)
    record_api_mutation(
        user=_user,
        endpoint="/api/calibrator/save",
        action="save_calibrator",
        status="applied" if result.get("ok", True) else "blocked",
        result={"bucket_count": len(req.buckets), **result},
    )
    return result


@router.post("/load")
def load_calibrator(_user: RequireUser)-> dict:
    try:
        result = load()
        record_api_mutation(
            user=_user,
            endpoint="/api/calibrator/load",
            action="load_calibrator",
            status="applied" if result.get("ok", True) else "blocked",
            result=result,
        )
        return result
    except FileNotFoundError as e:
        record_api_mutation(
            user=_user,
            endpoint="/api/calibrator/load",
            action="load_calibrator",
            status="blocked",
            result={"error": "not_found"},
        )
        raise HTTPException(status_code=404, detail={"error": "not_found", "msg": str(e)})
