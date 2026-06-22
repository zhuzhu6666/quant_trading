"""GET/PUT /api/config."""
from fastapi import APIRouter, HTTPException
from backend.core.auth import RequireUser
from pydantic import BaseModel, Field

from backend.services.config_service import get_config, patch_runtime_config, put_config

router = APIRouter(prefix="/api/config", tags=["config"])


class PutRequest(BaseModel):
    yaml: str


class RuntimePatchRequest(BaseModel):
    patch: dict[str, object] = Field(default_factory=dict)


@router.get("")
def read(_user: RequireUser) -> dict:
    return get_config()


@router.put("")
def write(_user: RequireUser, req: PutRequest) -> dict:
    try:
        return put_config(req.yaml)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})


@router.patch("/runtime")
def patch_runtime(_user: RequireUser, req: RuntimePatchRequest) -> dict:
    try:
        return patch_runtime_config(req.patch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})
