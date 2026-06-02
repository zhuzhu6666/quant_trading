"""config/__init__.py — flat-constant shim for legacy scripts

新代码应直接读 config/settings.yaml (嵌套结构)。
本 shim 仅为老 scripts (fetch_mt5_data.py) 提供向后兼容常量:
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, SYMBOL, TIMEFRAME, DATA_DIR
    INITIAL_CASH, COMMISSION, SLIPPAGE
"""
from __future__ import annotations

import os
import pathlib

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_HERE = pathlib.Path(__file__).parent
_YAML = _HERE / "settings.yaml"

if yaml is not None and _YAML.exists():
    _cfg = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
else:
    _cfg = {}

# ── MT5 ────────────────────────────────────────────────────────────────────
MT5_LOGIN: int = int(_cfg.get("mt5", {}).get("account", 9823690))
MT5_PASSWORD: str = os.environ.get(
    _cfg.get("mt5", {}).get("password_env", "MT5_PASSWORD"), ""
)
MT5_SERVER: str = _cfg.get("mt5", {}).get("server", "Bybit-Live-2")
SYMBOL: str = _cfg.get("mt5", {}).get("symbol", "XAUUSD+")

# ── 数据 ──────────────────────────────────────────────────────────────────
DATA_DIR: str = _cfg.get("data", {}).get("db_path", "data/")
TIMEFRAME: str = "M15"  # paper/main 默认, 见 config/settings.yaml bar_timeframes

# ── 交易成本 (老 scripts 用) ─────────────────────────────────────────────
COMMISSION: float = float(_cfg.get("commission", {}).get("value", 6.0))
SLIPPAGE: float = float(_cfg.get("execution", {}).get("slippage_value", 0.02))

# ── 账户 (老 scripts 用, 现在用 risk.position.risk_per_trade_pct) ─────────
INITIAL_CASH: float = 500.0  # paper 默认, 见 config/settings.yaml → data
