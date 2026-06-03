"""scripts/drift_research_daemon.py - PR-3.2 漂移 -> 触发 re-search

设计:
- 读取 SEVERE_DRIFT 事件 (从 main.py 运行时直接回调, 或从 log 文件读)
- 触发 auto_discover_daemon.py 子进程跑 GP 重新发现
- 每次触发落盘 audit log
- 同一个 model_name 1 小时内最多触发 1 次 (避免 spam)

两种模式:
1. daemon 模式: 长跑, 监听共享文件 / 事件总线
2. once 模式 (cron): 读 drift_event.json, 有事件就 re-search

CLI:
  python scripts/drift_research_daemon.py --mode once --drift-event data/charts/drift_event.json
  python scripts/drift_research_daemon.py --mode test  # 模拟一次 SEVERE_DRIFT, 验证触发
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("drift_research")

# 同一 model 1 小时内最多触发 1 次
RESEARCH_COOLDOWN_SEC = 3600
TRIGGER_LOG = PROJECT_ROOT / "data" / "charts" / "drift_research_triggers.jsonl"


def should_research(model_name: str, now: float) -> bool:
    """检查 cooldown."""
    if not TRIGGER_LOG.exists():
        return True
    last_ts = 0.0
    with TRIGGER_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line.strip())
                if ev.get("model_name") == model_name:
                    last_ts = max(last_ts, ev.get("ts", 0))
            except json.JSONDecodeError:
                continue
    return (now - last_ts) >= RESEARCH_COOLDOWN_SEC


def log_trigger(event: dict):
    TRIGGER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRIGGER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def trigger_research(model_name: str, level: str, gap: float, msg: str,
                      n_bars: int = 5000, pop: int = 50, gen: int = 30,
                      auto_register: bool = True) -> dict:
    """
    触发 auto_discover_daemon.py 子进程, 跑 GP 重新发现.
    返回触发结果 dict (含子进程 stdout/stderr 摘要).
    """
    cmd = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "auto_discover_daemon.py"),
        "--n-bars", str(n_bars),
        "--pop", str(pop),
        "--gen", str(gen),
        "--top-k", "5",
        "--score-threshold", "50",
    ]
    if not auto_register:
        cmd.append("--dry-run")

    logger.info(f"Triggering re-search: {cmd}")
    t0 = _time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=600,
        )
        elapsed = _time.time() - t0
        out_tail = (result.stdout or "")[-500:]
        err_tail = (result.stderr or "")[-200:]
        ok = result.returncode == 0
        return {
            "ok": ok, "elapsed_sec": round(elapsed, 1),
            "returncode": result.returncode,
            "stdout_tail": out_tail, "stderr_tail": err_tail,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout 600s", "elapsed_sec": 600}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def make_drift_handler(n_bars: int = 5000, pop: int = 50, gen: int = 30):
    """
    工厂: 构造一个 drift_handler, 给 MetaLearnerMonitor 注册用.
    """
    def handler(model_name: str, level: str, gap: float, msg: str):
        now = _time.time()
        if not should_research(model_name, now):
            logger.info(f"cooldown active for {model_name}, skip re-search")
            return
        result = trigger_research(model_name, level, gap, msg, n_bars, pop, gen)
        event = {
            "ts": now, "ts_iso": datetime.now(timezone.utc).isoformat(),
            "model_name": model_name, "level": level, "gap": gap, "msg": msg,
            "result": result,
        }
        log_trigger(event)
        logger.info(f"Re-search triggered for {model_name}: ok={result.get('ok')}, "
                    f"elapsed={result.get('elapsed_sec')}s")
    return handler


def main():
    parser = argparse.ArgumentParser(description="Drift -> re-search daemon")
    parser.add_argument("--mode", choices=["once", "test"], default="once",
                        help="once: 读 drift_event.json; test: 模拟一次触发")
    parser.add_argument("--drift-event", default="data/charts/drift_event.json",
                        help="drift 事件 JSON 路径 (mode=once)")
    parser.add_argument("--n-bars", type=int, default=5000)
    parser.add_argument("--pop", type=int, default=50)
    parser.add_argument("--gen", type=int, default=30)
    parser.add_argument("--no-register", action="store_true")
    args = parser.parse_args()

    if args.mode == "test":
        logger.info("=== TEST mode: 模拟一次 SEVERE_DRIFT ===")
        result = trigger_research("xgboost_test", "SEVERE_DRIFT", 0.15,
                                   "test trigger", args.n_bars, args.pop, args.gen,
                                   auto_register=not args.no_register)
        log_trigger({
            "ts": _time.time(), "ts_iso": datetime.now(timezone.utc).isoformat(),
            "model_name": "xgboost_test", "level": "SEVERE_DRIFT",
            "gap": 0.15, "msg": "test trigger", "result": result,
        })
        print()
        print("=" * 60)
        print("DRIFT RE-SEARCH (TEST)")
        print("=" * 60)
        print(f"  result: {result}")
        print("=" * 60)
        return

    # once: 读 drift_event.json
    drift_path = PROJECT_ROOT / args.drift_event
    if not drift_path.exists():
        logger.info(f"No drift event at {drift_path}, nothing to do")
        return
    try:
        event = json.loads(drift_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid drift event JSON: {e}")
        return

    model = event.get("model_name", "unknown")
    level = event.get("level", "DRIFT")
    gap = event.get("gap", 0)
    msg = event.get("msg", "")

    if level != "SEVERE_DRIFT":
        logger.info(f"Drift level is {level}, not SEVERE_DRIFT, skip re-search")
        return

    if not should_research(model, _time.time()):
        logger.info(f"Cooldown active for {model}, skip")
        return

    result = trigger_research(model, level, gap, msg, args.n_bars, args.pop, args.gen,
                               auto_register=not args.no_register)
    log_trigger({
        "ts": _time.time(), "ts_iso": datetime.now(timezone.utc).isoformat(),
        "model_name": model, "level": level, "gap": gap, "msg": msg,
        "result": result,
    })

    # 处理完删除 event 文件 (避免重复)
    drift_path.unlink()

    print()
    print("=" * 60)
    print(f"DRIFT RE-SEARCH (level={level}, model={model})")
    print("=" * 60)
    print(f"  ok={result.get('ok')}  elapsed={result.get('elapsed_sec')}s")
    print("=" * 60)


if __name__ == "__main__":
    main()