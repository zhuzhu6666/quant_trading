#!/usr/bin/env python3
"""scripts/fetch_events_calendar.py — 从 ForexFactory JSON 拉取经济日历并入库

数据源: nfs.faireconomy.media (ForexFactory 数据的 JSON 镜像)
- 仅提供本周数据, 每天运行一次即可覆盖未来事件
- 返回干净 JSON, 无需 headless browser / cloudscraper

用法:
    python scripts/fetch_events_calendar.py              # 拉取本周并入库
    python scripts/fetch_events_calendar.py --dry-run    # 只打印不写库
    python scripts/fetch_events_calendar.py --weeks 2    # 拉取本周+下周 (如可用)

入库策略:
    UPSERT by (date, type): 新事件插入, 已有事件更新 description/importance
    不删除旧事件 (历史数据保留)
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB = str(PROJECT_ROOT / "data" / "market_data.db")

# ── API 端点 ──
BASE_URL = "https://nfs.faireconomy.media"
ENDPOINTS = [
    f"{BASE_URL}/ff_calendar_thisweek.json",
    # nextweek 在某些时期可用, 不可用时 graceful skip
    f"{BASE_URL}/ff_calendar_nextweek.json",
]

# ── 事件类型映射 (title 关键词 → type) ──
# 注意: 顺序很重要, 先匹配更具体的关键词
EVENT_TYPE_MAP = [
    # ── FOMC (HIGH=3, 但仅限 actual decision/minutes) ──
    ("FOMC RATE DECISION",  "FOMC", 3),   # 实际决议
    ("FOMC STATEMENT",      "FOMC", 3),
    ("FEDERAL RESERVE RATE", "FOMC", 3),
    ("FOMC MINUTES",        "FOMC", 3),
    # FOMC 成员讲话 → MEDIUM=2 (不是实际决议)
    ("FOMC MEMBER",         "FOMC", 2),
    ("FED CHAIR",           "FOMC", 2),
    ("FEDERAL RESERVE CHAIR", "FOMC", 2),
    # 通用 FOMC 关键词 → HIGH (兜底)
    ("FOMC",                "FOMC", 3),

    # ── NFP (HIGH=3) ──
    # 注意: ADP 要在 NFP 之前匹配 (ADP 是私人报告, importance=2)
    ("ADP NON-FARM",       None,   2),   # ADP 非农 → 特殊处理
    ("ADP NONFARM",        None,   2),
    ("NON-FARM PAYROLL",    "NFP",  3),
    ("NONFARM PAYROLL",     "NFP",  3),
    ("NON-FARM EMPLOYMENT", "NFP",  3),
    ("EMPLOYMENT SITUATION", "NFP", 3),

    # ── CPI (HIGH=3) ──
    ("CONSUMER PRICE INDEX", "CPI", 3),
    ("CORE CPI",            "CPI",  3),
    # 仅 "CPI" 关键词 → 需要排除 "PPI" 等
    (" CPI ",               "CPI",  3),

    # ── PCE (MEDIUM=2) ──
    ("PERSONAL CONSUMPTION", "PCE", 2),
    ("CORE PCE",            "PCE",  2),
    ("PCE PRICE",           "PCE",  2),
]

# 其他高影响 USD 事件也入库 (importance=2)
OTHER_HIGH_USD_KEYWORDS = [
    "RETAIL SALES",
    "ISM MANUFACTURING",
    "ISM SERVICES",
    "UNEMPLOYMENT RATE",
    "PARTICIPATION RATE",
    "AVERAGE HOURLY EARNINGS",
    "ADP NONFARM",
    "ADP NON-FARM",
    "JOLTS",
    "CONSUMER CONFIDENCE",
    "UMICH CONSUMER SENTIMENT",
    "DURABLE GOODS",
    "GDP",
]

logger = logging.getLogger(__name__)


def _match_event_type(title: str) -> tuple[str, int] | None:
    """将事件标题映射到 (type, importance)"""
    t = title.upper()
    for keyword, evt_type, imp in EVENT_TYPE_MAP:
        if keyword in t:
            return evt_type, imp
    return None


def _is_high_impact_usd(title: str, impact: str) -> bool:
    """判断是否为高影响 USD 事件 (即使不是 FOMC/NFP/CPI/PCE)"""
    if impact != "High":
        return False
    t = title.upper()
    return any(kw in t for kw in OTHER_HIGH_USD_KEYWORDS)


def _parse_dt_to_utc(date_str: str) -> datetime | None:
    """ISO 8601 带时区 → UTC datetime"""
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_events(weeks: int = 1, max_retries: int = 3) -> list[dict]:
    """从 API 拉取经济日历事件, 带 retry + backoff"""
    all_events = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.forexfactory.com/",
    }

    for i, url in enumerate(ENDPOINTS):
        if i >= weeks:
            break

        for attempt in range(max_retries):
            try:
                logger.info(f"Fetching: {url} (attempt {attempt + 1})")
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 429:
                    wait = 2 ** attempt * 5  # 5s, 10s, 20s
                    logger.warning(f"  → 429 Too Many Requests, retry in {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                events = resp.json()
                logger.info(f"  → {len(events)} events")
                all_events.extend(events)
                break
            except requests.RequestException as e:
                logger.warning(f"  → Failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt * 3)
            except json.JSONDecodeError as e:
                logger.warning(f"  → Invalid JSON: {e}")
                break

    return all_events


def parse_and_filter(raw_events: list[dict]) -> list[dict]:
    """解析 + 过滤 USD 事件"""
    results = []
    seen = set()

    for ev in raw_events:
        country = ev.get("country", "")
        impact = ev.get("impact", "")
        title = ev.get("title", "")
        date_str = ev.get("date", "")

        # 仅 USD 事件
        if country != "USD":
            continue

        # 解析时间
        dt_utc = _parse_dt_to_utc(date_str)
        if dt_utc is None:
            logger.debug(f"  Skip (bad date): {title} → {date_str}")
            continue

        date_key = dt_utc.strftime("%Y-%m-%d")

        # 映射事件类型
        match = _match_event_type(title)
        if match:
            evt_type, importance = match
            # None type → 用标题作为 type (如 ADP)
            if evt_type is None:
                evt_type = title.strip()[:50]
        elif _is_high_impact_usd(title, impact):
            # 其他高影响 USD 事件
            evt_type = title.strip()[:50]  # 截断过长标题
            importance = 2
        elif impact == "High":
            # 未识别但标记为 High 的事件
            evt_type = title.strip()[:50]
            importance = 2
        else:
            # Medium/Low → 跳过
            continue

        # 去重 (同一天同类型只保留一条)
        dedup_key = (date_key, evt_type)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        results.append({
            "date": date_key,
            "type": evt_type,
            "description": title.strip(),
            "importance": importance,
        })

    return results


def upsert_events(events: list[dict], dry_run: bool = False) -> dict:
    """UPSERT 事件到 SQLite"""
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "total": len(events)}

    if dry_run:
        for ev in events:
            logger.info(f"  [DRY-RUN] {ev['date']} {ev['type']:20s} "
                        f"imp={ev['importance']}  {ev['description']}")
        return stats

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    for ev in events:
        # 检查是否已存在
        cur.execute(
            "SELECT description, importance FROM events "
            "WHERE date=? AND type=?",
            (ev["date"], ev["type"]),
        )
        existing = cur.fetchone()

        if existing is None:
            # 新事件 → INSERT
            cur.execute(
                "INSERT INTO events (date, type, description, importance) "
                "VALUES (?, ?, ?, ?)",
                (ev["date"], ev["type"], ev["description"], ev["importance"]),
            )
            stats["inserted"] += 1
        elif existing[0] != ev["description"] or existing[1] != ev["importance"]:
            # 已有但内容变化 → UPDATE
            cur.execute(
                "UPDATE events SET description=?, importance=? "
                "WHERE date=? AND type=?",
                (ev["description"], ev["importance"], ev["date"], ev["type"]),
            )
            stats["updated"] += 1
        else:
            stats["skipped"] += 1

    conn.commit()
    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="拉取经济日历并入库")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印, 不写库")
    parser.add_argument("--weeks", type=int, default=1,
                        help="拉取几周 (默认 1, nextweek 端点可能不可用)")
    parser.add_argument("--verbose", action="store_true",
                        help="显示调试信息")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 拉取
    raw = fetch_events(weeks=args.weeks)
    if not raw:
        logger.warning("未获取到任何事件 (网络问题?)")
        sys.exit(1)

    # 解析 + 过滤
    events = parse_and_filter(raw)
    logger.info(f"过滤后: {len(events)} 个 USD 事件 "
                f"(原始 {len(raw)} 个)")

    if not events:
        logger.info("无新事件需要入库")
        sys.exit(0)

    # 按日期排序
    events.sort(key=lambda e: (e["date"], e["type"]))

    # 入库
    stats = upsert_events(events, dry_run=args.dry_run)

    # 汇报
    action = "Would" if args.dry_run else "Did"
    logger.info(
        f"\n{'='*50}\n"
        f"  Economic Calendar Update\n"
        f"  {action} insert: {stats['inserted']}\n"
        f"  {action} update: {stats['updated']}\n"
        f"  Skipped (no change): {stats['skipped']}\n"
        f"  Total processed: {stats['total']}\n"
        f"{'='*50}"
    )

    # 打印即将发生的事件
    now = datetime.now(timezone.utc)
    upcoming = [
        e for e in events
        if e["date"] >= now.strftime("%Y-%m-%d")
    ]
    if upcoming:
        logger.info("\nUpcoming events:")
        for ev in upcoming[:10]:
            logger.info(f"  {ev['date']}  {ev['type']:20s}  "
                        f"imp={ev['importance']}  {ev['description']}")


if __name__ == "__main__":
    main()
