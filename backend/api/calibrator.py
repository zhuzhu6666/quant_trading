"""GET/POST /api/calibrator."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.calibrator_service import get_status, load, save

router = APIRouter(prefix="/api/calibrator", tags=["calibrator"])


class SaveRequest(BaseModel):
    buckets: list[dict]


@router.get("")
def read() -> dict:
    return get_status()


@router.post("/save")
def save_buckets(req: SaveRequest) -> dict:
    return save(req.buckets)


@router.post("/load")
def load_calibrator() -> dict:
    try:
        return load()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail={"error": "not_found", "msg": str(e)})
