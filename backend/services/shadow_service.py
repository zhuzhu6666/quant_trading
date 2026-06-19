"""Shadow factor service — read/promote/demote via alpha/registry_adapter.

audit 2026-06-08: 之前版本写到 shadow_factors.jsonl, 但业务代码 (策略的
_load_shadow_factors) 真正读的是 factor_lifecycle_log.jsonl, promote/demote
对 strategy 完全静默无效. 重写后直接用 RegistryAdapter:
  - promote: shadow → discovered, 改 _meta + 落 lifecycle log
  - demote:  unregister, builtin 受保护
  - list:    RegistryAdapter.list_by_source(SOURCE_SHADOW) + get_meta 拿详情
"""
from __future__ import annotations

import logging
from typing import Any

from alpha.registry_adapter import (
    RegistryAdapter,
    SOURCE_SHADOW,
    SOURCE_DISCOVERED,
)

logger = logging.getLogger(__name__)


def _get_adapter() -> RegistryAdapter:
    """每次调用都新建一个 RegistryAdapter — 简单可靠.
    内部 _meta dict 是 in-process state, 跟其它 service 共享 (singleton 不会).
    注意: persistent_registry 启动时也会构造一个, 不会冲突 (RegistryAdapter
    只读 factor_registry 共享 dict, _meta 各管各的)."""
    return RegistryAdapter()


def list_shadows() -> list[dict]:
    """返回所有 SOURCE_SHADOW 因子 + 它们的 meta 信息."""
    adapter = _get_adapter()
    names = adapter.list_by_source(SOURCE_SHADOW)
    out: list[dict] = []
    for name in names:
        meta = adapter.get_meta(name)
        ts = meta.get("register_time", 0)
        from datetime import datetime, timezone
        ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
        out.append({
            "name": name,
            "status": meta.get("source", SOURCE_SHADOW),
            "source": meta.get("source", SOURCE_SHADOW),
            "ts": ts_iso,
            "expr": meta.get("description", ""),
            "description": meta.get("description", ""),
        })
    return out


def promote(name: str) -> dict:
    """把 shadow 因子晋升到 discovered (改 source + 落 lifecycle log)."""
    adapter = _get_adapter()
    meta = adapter.get_meta(name)
    if not meta:
        return {"name": name, "ok": False, "error": f"factor {name!r} not in registry"}
    if meta.get("source") == SOURCE_DISCOVERED:
        return {"name": name, "ok": True, "new_status": SOURCE_DISCOVERED, "msg": "already discovered"}
    ok = adapter.promote(name, new_source=SOURCE_DISCOVERED, reason="manual promote via /api/shadow/promote")
    if not ok:
        return {"name": name, "ok": False, "error": "promote() returned False (builtin or already in target state)"}
    return {"name": name, "ok": True, "new_status": SOURCE_DISCOVERED}


def demote(name: str) -> dict:
    """从 registry 移除一个非 builtin 因子.
    builtin 会受 RegistryAdapter.unregister 保护, 自动跳过."""
    adapter = _get_adapter()
    meta = adapter.get_meta(name)
    if not meta:
        return {"name": name, "ok": False, "error": f"factor {name!r} not in registry"}
    source = meta.get("source", "builtin")
    if source == "builtin":
        return {"name": name, "ok": False, "error": "cannot demote builtin factor (protected)"}
    ok = adapter.unregister(name, reason="manual demote via /api/shadow/demote")
    if not ok:
        return {"name": name, "ok": False, "error": "unregister() returned False"}
    return {"name": name, "ok": True, "new_status": "removed"}
