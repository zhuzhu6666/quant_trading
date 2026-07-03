"""GET/PUT /api/config."""
from fastapi import APIRouter, Header, HTTPException
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
def write(
    _user: RequireUser,
    req: PutRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    try:
        return put_config(req.yaml, x_confirm=x_confirm, user=_user)
    except PermissionError as e:
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": str(e)},
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})


@router.patch("/runtime")
def patch_runtime(
    _user: RequireUser,
    req: RuntimePatchRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    try:
        return patch_runtime_config(req.patch, x_confirm=x_confirm, user=_user)
    except PermissionError as e:
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": str(e)},
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})
