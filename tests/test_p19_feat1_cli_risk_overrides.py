"""
tests/test_p19_feat1_cli_risk_overrides.py — FEAT-1 fix

引自 framework_audit_20260604.md FEAT-1:
main.py 没有 CLI flag 给风险参数, 改个 max_daily_loss_pct 要改代码。
修复: 加 --max-daily-loss-pct / --max-consecutive-loss / --single-risk-usd /
       --volatility-mult 4 个 CLI flag, 默认 None (用 YAML/settings.yaml 兜底)。
"""
import inspect

import pytest

import main


def test_feat1_cli_has_risk_override_flags():
    """FEAT-1: main.py argparse 应当有 4 个风险参数 flag"""
    src = inspect.getsource(main._build_arg_parser if hasattr(main, '_build_arg_parser') else main.main)
    for flag in ("--max-daily-loss-pct", "--max-consecutive-loss",
                 "--single-risk-usd", "--volatility-mult"):
        assert flag in src, (
            f"FEAT-1 未生效: main.py 缺 {flag} CLI flag"
        )


def test_feat1_cli_defaults_to_none():
    """FEAT-1: 风险参数 CLI 默认值是 None, 让 YAML 兜底"""
    src = inspect.getsource(main.main)
    # 4 个 flag 都应当 default=None
    assert '--max-daily-loss-pct", type=float, default=None' in src, (
        "FEAT-1: --max-daily-loss-pct 应当 default=None"
    )
    assert '--single-risk-usd", type=float, default=None' in src
