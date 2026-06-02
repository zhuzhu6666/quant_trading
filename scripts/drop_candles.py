"""
drop_candles.py — 删 candles 僵尸表 (2026-06-02 23:40)

已确认:
- 2 个引用点 (regime.py:477 已改走 bars, backfill_strategy_perf.py:91-98 检测存在性 graceful 跳过)
- paper 跑通验证 regime 改写不挂
- 用户明确 yes DROP
"""
import sqlite3
import os

DB = "data/market_data.db"

# 备份 (保险, 删错可恢复)
backup = DB + ".pre_drop_candles.bak"
if not os.path.exists(backup):
    import shutil
    shutil.copy2(DB, backup)
    print(f"已备份到 {backup}")
else:
    print(f"备份已存在, 跳过: {backup}")

c = sqlite3.connect(DB)
print("\n=== 删前 ===")
for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    n = c.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
    print(f"  {t[0]}: {n} rows")

# 删 candles
c.execute("DROP TABLE candles")
c.commit()
# VACUUM 释放空间 (可选, 不影响功能)
c.execute("VACUUM")
c.commit()

print("\n=== 删后 ===")
for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    n = c.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
    print(f"  {t[0]}: {n} rows")

c.close()
print("\n✓ candles 表已删, 备份保留在", backup)
