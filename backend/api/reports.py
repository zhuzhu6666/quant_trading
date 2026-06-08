"""GET /api/reports + GET /api/reports/{name}."""
from fastapi import APIRouter, HTTPException

from backend.core.auth import RequireUser
from backend.services.report_service import list_reports, read_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
def list_(_user: RequireUser, kind: str | None = None) -> dict:
    return {"reports": list_reports(kind)}


@router.get("/{name}")
def read(_user: RequireUser, name: str) -> dict:
    try:
        return read_report(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail={"error": "not_found", "msg": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "msg": str(e)})
