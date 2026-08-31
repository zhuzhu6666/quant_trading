"""backend/core/env.py — 统一环境变量读取。

胶水收敛：先 os.environ，再回退读 ~/quant_trading/.env 文本。
所有手写 env 读取统一到此。
"""
from __future__ import annotations

import os
from pathlib import Path

# 服务端 .env 固定路径（任务要求 ~/quant_trading/.env）
_SERVER_ENV = Path.home() / "quant_trading" / ".env"
# 项目根兼容路径（本地开发/测试时可能以项目根 .env 为准）
_PROJECT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"


def get_env(key: str, default=None):
    """先取 os.environ，再回退读 .env 文本。"""
    value = os.environ.get(key)
    if value is not None:
        return value
    # 依次尝试服务端路径与项目根路径
    for env_path in (_SERVER_ENV, _PROJECT_ENV):
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
        except Exception:
            continue
    return default


def truthy_env(key: str, default=None) -> bool:
    """环境变量是否为真值：1/true/yes（大小写不敏感）。"""
    # 兼容旧调用 truthy_env(key, "1") 这类传 default 的场景
    val = get_env(key, default)
    return str(val).lower() in ("1", "true", "yes")
