"""scripts/load_gld_holdings_sec.py — 从 SEC EDGAR 直接下 GLD 10-Q 提取 oz (2026-06-03)

方法: 直接构造 URL pattern (不依赖 EDGAR 搜索结果)
  URL格式: https://www.sec.gov/Archives/edgar/data/1222333/{ACCESSION}/gld{PERIOD_END}_10q.htm
  报告期: 3/31, 6/30, 9/30, 12/31
  拉 2023Q1 ~ 2026Q1 (13 份 10-Q + 4 份 10-K)
"""
import re
import sys
import time
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.store import DataStore

CACHE_DIR = Path("data/sec_gld")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; zhu@quant.trading)"
CURL = ["curl", "-sL", "--max-time", "60", "-H", f"User-Agent: {UA}", "-H", "Accept: text/html,application/xhtml+xml"]

# 已知 accession (SEC 18位) 跟 period end 的对齐
# 从 EDGAR browse 抓到的: accession in filing order
FILINGS = [
    ("000143774926014926", "2026-03-31"),
    ("000143774926002987", "2025-12-31"),
    ("000143774925024998", "2025-09-30"),
    ("000143774925015077", "2025-06-30"),
    ("000143774925002865", "2025-03-31"),
    ("000143774924024842", "2024-09-30"),
    ("000143774924015320", "2024-06-30"),
    ("000143774924003442", "2024-03-31"),
    ("000143774923022288", "2023-09-30"),
    ("000143774923013346", "2023-06-30"),
    ("000143774923002856", "2023-03-31"),
]


def download_10q(accn: str, period_end: str) -> Path:
    """下载 gld{yyyymmdd}_10q.htm."""
    yyyymmdd = period_end.replace("-", "")
    out = CACHE_DIR / f"gld_{yyyymmdd}_10q.htm"
    if out.exists() and out.stat().st_size > 100000:
        return out
    url = f"https://www.sec.gov/Archives/edgar/data/1222333/{accn}/gld{yyyymmdd}_10q.htm"
    print(f"  {url[-80:]}")
    for attempt in range(5):
        r = subprocess.run(CURL + ["-o", str(out), url], capture_output=True, timeout=70)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 100000:
            time.sleep(1.0)
            return out
        time.sleep(3 * (attempt + 1))
    return None


def parse_10q(path: Path) -> dict:
    """从 10-Q 提取月末 ounces + shares."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-z#0-9]+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean)

    result = {"monthly": []}

    # 1. 季度末 total ounces (两处: latest + prior quarter)
    # "was XX,XXX,XXX.X ounces" 格式
    oz_matches = re.findall(
        r"([\d,]+\.\d)\s*ounces\s*,\s*100%",
        clean,
    )
    if len(oz_matches) >= 1:
        result["latest_quarter_oz"] = float(oz_matches[0].replace(",", ""))
    if len(oz_matches) >= 2:
        result["prior_quarter_oz"] = float(oz_matches[1].replace(",", ""))

    # 2. Shares outstanding
    sh_m = re.search(
        r"had\s+([\d,]+)\s*Shares\s*outstanding",
        clean,
    )
    if sh_m:
        result["shares_outstanding"] = int(sh_m.group(1).replace(",", ""))

    # 3. 月度 Oz Per Share 表
    # "1/1/26 to 1/31/26 ... 0.09194"
    monthly_pattern = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2})\s*(?:to|-)\s*(\d{1,2}/\d{1,2}/\d{2})"
        r"\s+[-\d\s,]+\s+([\d.]+)"
    )
    for m in monthly_pattern.finditer(clean):
        start_str, end_str, oz_str = m.group(1), m.group(2), m.group(3)
        try:
            oz_val = float(oz_str)
        except ValueError:
            continue
        if not (0.05 < oz_val < 0.15):  # GLD oz/share 通常 ~0.09
            continue
        # 解析日期 (MM/DD/YY 格式)
        parts = end_str.split("/")
        if len(parts) == 3:
            mm, dd, yy = parts
            if len(yy) == 2:
                yy = "20" + yy
            month_end = f"{yy}-{int(mm):02d}-{int(dd):02d}"
            result["monthly"].append({
                "month_end": month_end,
                "oz_per_share": oz_val,
            })

    return result


def main():
    print("=" * 78)
    print(" SEC EDGAR GLD Holdings Loader v2 (2026-06-03)")
    print("=" * 78)
    print(f" {len(FILINGS)} filings to download")
    print()

    all_monthly = []
    for accn, period_end in FILINGS:
        try:
            path = download_10q(accn, period_end)
            if not path:
                print(f"  {period_end}: download FAIL")
                continue
            data = parse_10q(path)
            sh = data.get("shares_outstanding")
            print(f"  {period_end}: sh={sh}, quarterly_oz={data.get('latest_quarter_oz')}, monthly={len(data['monthly'])}")
            for m in data["monthly"]:
                m["shares_outstanding"] = sh
                all_monthly.append(m)
            time.sleep(1.5)
        except Exception as e:
            print(f"  {period_end} FAIL: {type(e).__name__}: {e}")

    if not all_monthly:
        print("\nNo monthly data parsed. Trying alternative pattern...")
        # Fallback: at least get quarterly oz
        for accn, period_end in FILINGS:
            try:
                path = download_10q(accn, period_end)
                if not path:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                clean = re.sub(r"<[^>]+>", " ", text)
                clean = re.sub(r"\s+", " ", clean)
                oz_match = re.search(r"([\d,]+\.\d)\s*ounces", clean)
                sh_match = re.search(r"([\d,]+)\s*Shares\s*outstanding", clean)
                if oz_match:
                    all_monthly.append({
                        "month_end": period_end,
                        "oz_per_share": float(oz_match.group(1).replace(",", "")),
                        "shares_outstanding": int(sh_match.group(1).replace(",", "")) if sh_match else 350_000_000,
                    })
                    print(f"  {period_end}: quarterly oz={oz_match.group(1)}")
            except Exception as e:
                print(f"  {period_end}: {e}")

    print(f"\nTotal records: {len(all_monthly)}")
    if all_monthly:
        # 去重 + 排序
        seen = {}
        for r in all_monthly:
            k = r["month_end"]
            if k not in seen or len(r) > len(seen[k]):
                seen[k] = r
        unique = sorted(seen.values(), key=lambda x: x["month_end"])
        print(f"Unique: {len(unique)}")
        for r in unique:
            print(f"  {r['month_end']}: {r['oz_per_share']}")

        # 写库
        store = DataStore("data/market_data.db")
        inserted = 0
        for r in unique:
            sh = r.get("shares_outstanding") or 350_000_000
            # 如果是 monthly (oz_per_share < 1), 乘 shares 转 total oz
            oz_val = r["oz_per_share"]
            if oz_val < 1:  # monthly record, 乘 shares
                total_oz = oz_val * sh
            else:  # quarterly record, 就是总 oz
                total_oz = oz_val
            total_tonnes = total_oz / 32150.7
            store.insert_etf_holding(
                symbol="GLD",
                date=r["month_end"],
                total_tonnes=round(total_tonnes, 2),
                total_shares=sh,
                aum_usd=None,
            )
            inserted += 1
        print(f"\nInserted {inserted} GLD rows into etf_holdings")


if __name__ == "__main__":
    main()
