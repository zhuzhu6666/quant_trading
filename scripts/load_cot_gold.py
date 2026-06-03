"""scripts/load_cot_gold.py — 从 CFTC disagg COT 加载黄金持仓 (2026-06-03)

数据源: CFTC https://www.cftc.gov/files/dea/history/fut_disagg_txt_<year>.zip
  - 字段: 见 scripts/load_cot_gold.py 解析逻辑
  - 周度, 黄金 COT 报告 1 周延迟
  - 4 类持仓者:
      Prod_Merc (Producer/Merchant, 商业/对冲)
      Swap (互换)
      M_Money (Managed Money, 投机/非商业)  ← 重点
      Other_Rept (Other Reportables)
  - 派生指标 (写入数据库时计算):
      mm_net = M_Money_Long - M_Money_Short
      mm_net_pct_oi = mm_net / Open_Interest
      pm_net = Prod_Merc_Long - Prod_Merc_Short
      swap_net

覆盖: 2009-2026 (18 年, 950+ 周)
本地缓存: data/cot/fut_disagg_txt_<year>.zip (避免重复下载)
"""
import os
import sys
import ssl
import time
import zipfile
import urllib.request
import urllib.error
import socket
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.store import DataStore


# 关 SSL verify, 设 timeout
ssl._create_default_https_context = ssl._create_unverified_context
socket.setdefaulttimeout(60)


CACHE_DIR = Path("data/cot")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def download_year(year: int, max_retries: int = 3) -> Path:
    """下载 CFTC disagg COT zip, 缓存到本地. retry 3 次."""
    zip_path = CACHE_DIR / f"fut_disagg_txt_{year}.zip"
    if zip_path.exists() and zip_path.stat().st_size > 100_000:
        return zip_path
    url = f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
    print(f"Downloading {url} ...")
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            with open(zip_path, "wb") as f:
                f.write(data)
            print(f"  saved {len(data):,} bytes → {zip_path}")
            return zip_path
        except Exception as e:
            print(f"  attempt {attempt}/{max_retries} FAIL: {type(e).__name__}: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to download {url} after {max_retries} retries")


def parse_year(zip_path: Path) -> pd.DataFrame:
    """解压并抽出所有 GOLD 行 → DataFrame."""
    rows = []
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(".txt")]
        if not names:
            return pd.DataFrame()
        with z.open(names[0]) as f:
            for line_bytes in f:
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # GOLD - COMMODITY EXCHANGE INC. 是黄金期货
                if line.startswith('"GOLD - COMMODITY'):
                    cols = line.split(",")
                    if len(cols) < 15:
                        continue
                    rows.append({
                        "report_date": cols[2],  # Report_Date_as_YYYY-MM-DD
                        "open_interest": int(cols[7]),
                        "pm_long": int(cols[8]),
                        "pm_short": int(cols[9]),
                        "pm_spread": int(cols[10]),
                        "swap_long": int(cols[11]),
                        "swap_short": int(cols[12]),
                        "mm_long": int(cols[13]),
                        "mm_short": int(cols[14]),
                        "mm_spread": int(cols[15]),
                        "other_long": int(cols[16]),
                        "other_short": int(cols[17]),
                        "other_spread": int(cols[18]),
                    })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["report_date"] = pd.to_datetime(df["report_date"])
        df = df.sort_values("report_date").reset_index(drop=True)
    return df


def main():
    years = list(range(2009, 2027))  # 2009-2026, CFTC disagg 格式从 2009 开始
    all_dfs = []
    for y in years:
        try:
            zp = download_year(y)
            df = parse_year(zp)
            if not df.empty:
                print(f"  {y}: {len(df)} GOLD rows, range {df['report_date'].iloc[0].date()} → {df['report_date'].iloc[-1].date()}")
                all_dfs.append(df)
        except Exception as e:
            print(f"  {y} FAIL: {type(e).__name__}: {e}")

    if not all_dfs:
        print("ERROR: no data loaded")
        return

    df_all = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset=["report_date"]).sort_values("report_date").reset_index(drop=True)
    print(f"\nTotal GOLD COT rows: {len(df_all)}")
    print(f"Range: {df_all['report_date'].iloc[0].date()} → {df_all['report_date'].iloc[-1].date()}")
    print(f"Years covered: {(df_all['report_date'].iloc[-1] - df_all['report_date'].iloc[0]).days / 365.25:.1f} years")

    # 写库
    store = DataStore("data/market_data.db")
    n = 0
    for _, row in df_all.iterrows():
        store.insert_cot_gold(
            report_date=row["report_date"].strftime("%Y-%m-%d"),
            open_interest=int(row["open_interest"]),
            mm_long=int(row["mm_long"]),
            mm_short=int(row["mm_short"]),
            mm_spread=int(row["mm_spread"]),
            pm_long=int(row["pm_long"]),
            pm_short=int(row["pm_short"]),
            swap_long=int(row["swap_long"]),
            swap_short=int(row["swap_short"]),
            other_long=int(row["other_long"]),
            other_short=int(row["other_short"]),
        )
        n += 1
    print(f"Inserted {n} rows into cot_gold")

    # 验证
    import sqlite3
    con = sqlite3.connect("data/market_data.db")
    print(f"cot_gold count: {con.execute('SELECT COUNT(*) FROM cot_gold').fetchone()[0]}")
    print(f"latest 3:")
    for r in con.execute("SELECT report_date, open_interest, mm_long, mm_short, mm_long-mm_short AS mm_net FROM cot_gold ORDER BY report_date DESC LIMIT 3"):
        print(f"  {r}")


if __name__ == "__main__":
    main()
