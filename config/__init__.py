"""config/__init__.py — flat-constant shim for legacy scripts

新代码应直接读 config/settings.yaml (嵌套结构)。
本 shim 仅为老 scripts (fetch_mt5_data.py) 提供向后兼容常量:
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, SYMBOL, TIMEFRAME, DATA_DIR
    INITIAL_CASH, COMMISSION, SLIPPAGE

P1 (audit 2026-06-04): 新增 load_config() + cfg_get() 供新代码用。
"""
from __future__ import annotations

import os
import pathlib
import re

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


# ── P1 (audit 2026-06-04) ────────────────────────────────────────────────
def load_config(path: str | None = None) -> dict:
    """读 yaml 配置,返回嵌套 dict。env var 展开 ${VAR} 形式。

    找不到文件 → 返回空 dict(不抛)。
    """
    p = pathlib.Path(path) if path else _YAML
    if not p.exists() or yaml is None:
        return {}
    raw = p.read_text(encoding="utf-8")
    # env var 展开: ${VAR_NAME} -> os.environ['VAR_NAME'];未设置则保留原样
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), raw)
    return yaml.safe_load(raw) or {}


def cfg_get(cfg: dict, *keys, default=None, override=None):
    """安全 nested get + override 机制。

    用法:
        cfg_get(CFG, "risk", "max_daily_loss_pct")
            → 5.0  (from settings.yaml)
        cfg_get(CFG, "risk", "max_daily_loss_pct", override=10.0)
            → 10.0  (调优值, override 优先)

    override 设计目的: settings.yaml 是默认配置, 但 main.py 里有人调过
    的值需要"override 优先"地显示出来, 让读者一眼看到"YAML 写 X, 实际
    跑 Y, 为什么"。
    """
    if override is not None:
        return override
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default
