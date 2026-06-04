"""
tests/test_p1_yaml_loader.py — P1 fix: config loader

引自 framework_audit_20260604.md ARCH-2 + FOOTGUN-1 + BUG-9 同一根因:
config/settings.yaml 从未被加载,改 yaml 不生效。P1 引入 load_config()
+ cfg_get() helper, main.py 显式注入。

本文件 5 个 case:
  - load_config 读 yaml 返回嵌套 dict
  - cfg_get 安全 nested get
  - cfg_get 缺键时返回 default
  - env var 展开 ${VAR} 形式
  - settings.yaml 里 risk.max_daily_loss_pct=5.0 可被读到
"""
import os
import sys
from pathlib import Path

# conftest 已经把 PROJECT_ROOT 加到 sys.path
from config import load_config, cfg_get


def test_load_config_reads_yaml():
    """P1: load_config 应当能读 settings.yaml 并返回嵌套 dict"""
    cfg = load_config()
    assert isinstance(cfg, dict)
    # 顶层键
    assert "system" in cfg
    assert "mt5" in cfg
    assert "risk" in cfg


def test_cfg_get_nested():
    """P1: cfg_get 应能 safe get 嵌套键"""
    cfg = load_config()
    val = cfg_get(cfg, "risk", "max_daily_loss_pct")
    assert val == 5.0  # settings.yaml 写的是 5.0


def test_cfg_get_returns_default_on_missing_key():
    """P1: cfg_get 缺键时返回 default, 不抛 KeyError"""
    cfg = load_config()
    val = cfg_get(cfg, "risk", "nonexistent_key", default=42)
    assert val == 42

    val2 = cfg_get(cfg, "no", "such", "path", default=None)
    assert val2 is None


def test_cfg_get_with_override():
    """P1: cfg_get(..., override=X) 应在 yaml 有值时仍返回 override

    这是关键设计: YAML 写 5%, 但 main.py 调优后 override 到 10%,
    改 yaml 不会"穿透"到 10%, 但读者能一眼看出 override 关系。
    """
    cfg = load_config()
    yaml_val = cfg_get(cfg, "risk", "max_daily_loss_pct")  # 5.0
    override_val = cfg_get(cfg, "risk", "max_daily_loss_pct", override=10.0)
    assert yaml_val == 5.0
    assert override_val == 10.0  # override 优先


def test_load_config_handles_missing_file():
    """P1: 找不到 yaml 时不崩溃, 返回空 dict"""
    cfg = load_config(path="nonexistent.yaml")
    assert cfg == {}
    val = cfg_get(cfg, "any", "key", default="fallback")
    assert val == "fallback"


def test_env_var_expansion(monkeypatch, tmp_path):
    """P1: ${VAR_NAME} 形式应展开到 os.environ

    settings.yaml 里有 password_env: MT5_PASSWORD 这种机制,
    load_config 应做变量替换。
    """
    monkeypatch.setenv("TEST_PASSWORD", "secret123")
    yaml_content = "mt5:\n  password: ${TEST_PASSWORD}\n"
    p = tmp_path / "test_settings.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    cfg = load_config(path=str(p))
    assert cfg["mt5"]["password"] == "secret123"


def test_settings_yaml_risk_section_complete():
    """P1: settings.yaml 里的 risk 段应当被 load_config 完整保留

    这个 case 锁住 "YAML 改了, 跑测试能看见改了" 这个不变量。
    如果有人未来改 settings.yaml 但忘了 load, 这个 test 不会失败 —
    但 test_cfg_get_nested 已经验证了读取逻辑, 两者互补。
    """
    cfg = load_config()
    risk = cfg.get("risk", {})
    assert "max_daily_loss_pct" in risk
    assert "single_trade_risk_usd" in risk
    assert "circuit_breaker" in risk
    assert risk["circuit_breaker"]["consecutive_losses"] == 5
