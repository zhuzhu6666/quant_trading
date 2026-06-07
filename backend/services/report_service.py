"""Report service — list/read files under data/charts/."""
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from backend.core.paths import CHARTS_DIR


MAX_TXT_BYTES = 1_000_000  # 1MB cap for inline text
MAX_PNG_BYTES = 5_000_000  # 5MB cap for inline base64


def list_reports(kind: str | None = None) -> list[dict]:
    """List files under data/charts/ with metadata."""
    if not CHARTS_DIR.exists():
        return []
    out = []
    for p in sorted(CHARTS_DIR.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lstrip(".").lower()
        if kind and kind != "all" and ext != kind:
            continue
        out.append({
            "name": p.name,
            "path": str(p),
            "kind": ext,
            "size": p.stat().st_size,
            "modified_at": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        })
    return out


def read_report(name: str) -> dict:
    """Read a single report. Returns kind/content/data_url based on file type."""
    # Security: prevent path traversal
    safe_name = Path(name).name
    if safe_name != name or "/" in name or "\\" in name:
        raise ValueError("invalid name")
    p = CHARTS_DIR / safe_name
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"report not found: {safe_name}")
    ext = p.suffix.lstrip(".").lower()
    if ext == "txt":
        data = p.read_bytes()
        truncated = len(data) > MAX_TXT_BYTES
        if truncated:
            data = data[-MAX_TXT_BYTES:]
        return {
            "name": safe_name, "kind": "txt",
            "content": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
    elif ext == "json":
        return {"name": safe_name, "kind": "json", "content": json.loads(p.read_text(encoding="utf-8"))}
    elif ext == "png":
        data = p.read_bytes()
        if len(data) > MAX_PNG_BYTES:
            raise ValueError(f"png too large: {len(data)} bytes")
        b64 = base64.b64encode(data).decode("ascii")
        return {"name": safe_name, "kind": "png", "data_url": f"data:image/png;base64,{b64}"}
    elif ext == "npy":
        return {"name": safe_name, "kind": "npy", "content": f"<binary .npy file, {p.stat().st_size} bytes>"}
    else:
        # generic: try as text
        return {"name": safe_name, "kind": ext, "content": p.read_text(encoding="utf-8", errors="replace")[:MAX_TXT_BYTES]}
