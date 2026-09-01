"""EvolutionStory — 持久化"系统每天在干什么"事件流。

设计:
- append(event_type, payload) 一行一条 JSON 追加到 evolution_story.jsonl
- 提供 query(date) 接口回查(Phase 3 日报用)
- 与 metric Counter 联动:append 时自动 inc 对应 Counter
- S6 容量阀(2026-09-01): 文件按 50MB 轮转 + gzip 归档到 data/charts/archive，保留 30 天/500M 上限；PG runtime.evolution_events 为审计权威

不写日志(那走 structured_log);只写"业务事件":
  - factor_birth: 因子被注册
  - factor_death: 因子被 unregister
  - canary_promote: canary stage 推进
  - canary_rollback: canary 回滚
  - retire_pending: DECAYING 触发 grace
  - pnl_attribution: 每 N 笔 trade 归因
  - sync_recovered: 数据同步自愈
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# S6 容量阀常量
_MAX_BYTES = 50 * 1024 * 1024  # 50M 单文件上限，复用 logging 50M 阈值
_RETAIN_DAYS = 30
_MAX_ARCHIVE_BYTES = 500 * 1024 * 1024  # archive 总量 500M
_ARCHIVE_DIR = "data/charts/archive"

# D7 (audit-defects-2026-08-21 增补): pytest 进程(含测试拉起的子进程)默认
# 改道临时目录, 与 logging.py v10 对文件 sink 的隔离同一思路——生产事件流
# data/charts/evolution_story.jsonl 不再被测试进程追加污染。
def _default_story_path() -> str:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION"):
        tmp = Path(tempfile.gettempdir()) / "quant_evolution_story_pytest"
        return str(tmp / "evolution_story.jsonl")
    return "data/charts/evolution_story.jsonl"


def _is_pytest_path(path: str) -> bool:
    return "quant_evolution_story_pytest" in path


def _sweep_archive() -> None:
    """清理 archive：删 >30 天的 .gz，超 500M 时按最旧删。"""
    try:
        ad = Path(_ARCHIVE_DIR)
        if not ad.exists():
            return
        now = time.time()
        cutoff = now - _RETAIN_DAYS * 86400
        for p in ad.glob("evolution_story.*.jsonl.gz"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        # 总量控制
        files = sorted(ad.glob("evolution_story.*.jsonl.gz"), key=lambda x: x.stat().st_mtime)
        total = sum(f.stat().st_size for f in files if f.exists())
        while total > _MAX_ARCHIVE_BYTES and files:
            oldest = files.pop(0)
            try:
                sz = oldest.stat().st_size
                oldest.unlink()
                total -= sz
            except OSError:
                pass
    except Exception:
        logger.exception("EvolutionStory sweep archive failed")


class EvolutionStory:
    """Append-only JSONL 事件流。"""

    _instance: Optional["EvolutionStory"] = None
    _lock = threading.Lock()

    def __init__(self, path: Optional[str] = None) -> None:
        self._path: str = path or _default_story_path()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._fp_lock = threading.Lock()

    @classmethod
    def shared(cls, path: Optional[str] = None) -> "EvolutionStory":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(path)
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        with cls._lock:
            cls._instance = None

    @property
    def path(self) -> str:
        return self._path

    def _rotate_if_needed(self) -> None:
        """在 _fp_lock 内调用：超 50M 时 gzip 归档并截断。"""
        if _is_pytest_path(self._path):
            return
        p = Path(self._path)
        try:
            if not p.exists() or p.stat().st_size <= _MAX_BYTES:
                return
        except OSError:
            return
        try:
            ad = Path(_ARCHIVE_DIR)
            ad.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            base = ad / f"evolution_story.{ts}.jsonl.gz"
            target = base
            n = 1
            while target.exists():
                target = ad / f"evolution_story.{ts}_{n}.jsonl.gz"
                n += 1
            with open(p, "rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            # 截断原文件（原子：必须在压缩成功后）
            p.write_text("", encoding="utf-8")
            logger.info("EvolutionStory rotated %s -> %s", p, target)
        except Exception:
            logger.exception("EvolutionStory rotate failed")
            return
        _sweep_archive()

    def append(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """追加一条事件。

        自动加上 ts / ts_iso / event_type 字段。
        """
        record = {
            "ts": time.time(),
            "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
            "event_type": event_type,
        }
        if payload:
            record.update(payload)
        # 文件备份（S6 轮转闸在锁内）
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._fp_lock:
            try:
                self._rotate_if_needed()
            except Exception:
                logger.exception("EvolutionStory pre-rotate check failed")
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        # 主存储: PostgreSQL state schema.
        try:
            from backend.core.db import get_state_pg_conn
            conn = get_state_pg_conn()
            try:
                conn.execute(
                    "INSERT INTO evolution_events (timestamp, event_type, payload_json) VALUES (%s, %s, %s)",
                    (record["ts"], event_type, json.dumps(payload or {}, ensure_ascii=False))
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        # 联动 metric
        try:
            from backend.runtime.runtime_state import RuntimeState

            RuntimeState.shared().emit_metric("factor_lifecycle_events_total", {"event": event_type, "source": (payload or {}).get("source", "unknown")})
        except Exception:  # noqa: BLE001
            pass
        return record

    def query(self, since_ts: Optional[float] = None, event_type: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        """回查事件。since_ts 与 event_type 都可选;按时间倒序返回。"""
        if not os.path.exists(self._path):
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since_ts is not None and rec.get("ts", 0) < since_ts:
                        continue
                    if event_type is not None and rec.get("event_type") != event_type:
                        continue
                    out.append(rec)
        except OSError:
            logger.exception("EvolutionStory.query failed")
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:limit]

    def iter_all(self) -> Iterable[Dict[str, Any]]:
        """顺序迭代所有事件(给日报用)。"""
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

# S6 CLI: 手工 sweep / 预览容量，供日常运维与 feature_eng 复用
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EvolutionStory S6 maintenance")
    parser.add_argument("--sweep", action="store_true", help="清理 archive >30天 与 >500M")
    parser.add_argument("--status", action="store_true", help="显示文件与 PG 容量")
    parser.add_argument("--purge-pg", type=int, default=0, metavar="DAYS", help="裁剪 PG 中 DAYS 天前的行（默认 90）")
    args = parser.parse_args()
    if args.status or not (args.sweep or args.purge_pg):
        import pathlib as _pl
        p = _pl.Path(_default_story_path())
        sz = p.stat().st_size if p.exists() else 0
        arch = _pl.Path(_ARCHIVE_DIR)
        arch_sz = sum(f.stat().st_size for f in arch.glob("*.gz")) if arch.exists() else 0
        arch_cnt = len(list(arch.glob("*.gz"))) if arch.exists() else 0
        print(f"file: {p} {sz/1024/1024:.1f}M")
        print(f"archive: {arch_cnt} files {arch_sz/1024/1024:.1f}M / {_MAX_ARCHIVE_BYTES/1024/1024:.0f}M retain {_RETAIN_DAYS}d")
        try:
            from backend.core.db import get_state_pg_conn
            conn = get_state_pg_conn()
            cur = conn.execute("SELECT count(*) as c FROM runtime.evolution_events")
            print(f"pg rows: {cur.fetchone()[0]}")
            cur2 = conn.execute("SELECT min(timestamp) as min_ts, max(timestamp) as max_ts FROM runtime.evolution_events")
            r2 = cur2.fetchone()
            print(f"pg range: {r2[0]} .. {r2[1]}")
            conn.close()
        except Exception as e:
            print(f"pg status error: {e}")
    if args.sweep:
        _sweep_archive()
        print("sweep done")
    if args.purge_pg:
        days = args.purge_pg if args.purge_pg > 0 else 90
        try:
            from backend.core.db import get_state_pg_conn
            import time as _t
            cutoff = _t.time() - days*86400
            conn = get_state_pg_conn()
            cur = conn.execute("DELETE FROM runtime.evolution_events WHERE timestamp < %s", (cutoff,))
            n = cur.rowcount
            conn.commit()
            conn.close()
            print(f"purged {n} rows older than {days}d (cutoff {cutoff})")
        except Exception as e:
            print(f"purge error: {e}")
