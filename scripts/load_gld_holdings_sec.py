"""从 SEC EDGAR 拉 GLD ETF 持仓（动态查询 CIK 1222333）"""
import re, sys, time, json, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.db import DUCKDB_EXTERNAL
from data.external_schema import etf_release_at, record_raw_file
from data.store import DataStore

CACHE_DIR = Path("data/sec_gld")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
UA = "ZhuQuant zhu@zhuzhu666.icu"
CURL = ["curl", "-sL", "--max-time", "60", "--proxy", "http://127.0.0.1:7890", "-H", f"User-Agent: {UA}"]

def get_gld_filings():
    """从 SEC submissions API 获取 GLD 10-Q/10-K 列表"""
    url = "https://data.sec.gov/submissions/CIK0001222333.json"
    r = subprocess.run(CURL + [url], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        print(f"SEC API failed: {r.stderr[:200]}")
        return []
    data = json.loads(r.stdout)
    recent = data.get("filings", {}).get("recent", {})
    results = []
    forms = recent.get("form", [])
    accns = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primaries = recent.get("primaryDocument", [])
    for i in range(len(forms)):
        if forms[i] in ("10-Q", "10-K"):
            accn_clean = accns[i].replace("-", "")
            results.append({
                "filing_date": dates[i],
                "accession": accns[i],
                "accession_clean": accn_clean,
                "document": primaries[i],
            })
    return results

def download_filing(accn: str, accn_clean: str, doc: str) -> Path:
    """下载 filing 文档"""
    out = CACHE_DIR / doc
    if out.exists() and out.stat().st_size > 100000:
        return out
    url = f"https://www.sec.gov/Archives/edgar/data/1222333/{accn_clean}/{doc}"
    print(f"  downloading: {doc} ...")
    for attempt in range(3):
        r = subprocess.run(CURL + ["-o", str(out), url], capture_output=True, timeout=40)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 50000:
            time.sleep(0.5)
            return out
        print(f"    attempt {attempt+1} failed, size={out.stat().st_size if out.exists() else 0}")
        time.sleep(3)
    return None


def filing_url(accn_clean: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/1222333/{accn_clean}/{doc}"

def parse_10q(path: Path) -> list[dict]:
    """从 10-Q/10-K 提取月度 oz per share"""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-z#0-9]+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean)

    # 提取 shares outstanding
    sh = 350_000_000  # fallback
    sh_m = re.search(r"([\d,]+)\s*Shares\s*outstanding", clean)
    if sh_m:
        sh = int(sh_m.group(1).replace(",", ""))

    results = []
    # 月度 oz per share 表: "1/1/26 to 1/31/26 ... 0.09194"
    monthly_pattern = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2})\s*(?:to|-)\s*(\d{1,2}/\d{1,2}/\d{2})\s+[-\d\s,]+\s+([\d.]+)"
    )
    for m in monthly_pattern.finditer(clean):
        _, end_str, oz_str = m.group(1), m.group(2), m.group(3)
        try:
            oz_val = float(oz_str)
        except ValueError:
            continue
        if not (0.03 < oz_val < 0.20):
            continue
        parts = end_str.split("/")
        if len(parts) == 3:
            mm, dd, yy = parts
            if len(yy) == 2:
                yy = "20" + yy
            month_end = f"{yy}-{int(mm):02d}-{int(dd):02d}"
            results.append({"month_end": month_end, "oz_per_share": oz_val, "shares": sh})
    return results

def main():
    print("=" * 60)
    print(" GLD ETF Holdings Loader v3 (SEC API dynamic)")
    print("=" * 60)

    filings = get_gld_filings()
    print(f"Found {len(filings)} 10-Q/10-K filings\n")

    all_monthly = []
    for f in filings:
        path = download_filing(f["accession"], f["accession_clean"], f["document"])
        if not path:
            print(f"  {f['filing_date']} {f['document']}: download FAIL")
            continue
        records = parse_10q(path)
        for record in records:
            record["filing_date"] = f["filing_date"]
            record["source_url"] = filing_url(f["accession_clean"], f["document"])
        record_raw_file("sec_edgar", path, source_url=filing_url(f["accession_clean"], f["document"]))
        print(f"  {f['filing_date']} {f['document']}: {len(records)} monthly records, size={path.stat().st_size//1024}KB")
        all_monthly.extend(records)
        time.sleep(1)

    if not all_monthly:
        print("\nNo data extracted. Check SEC filings format.")
        return

    # 去重 + 排序
    seen = {}
    for r in all_monthly:
        k = r["month_end"]
        if k not in seen:
            seen[k] = r
        elif str(r.get("filing_date") or "") < str(seen[k].get("filing_date") or ""):
            seen[k] = r
    unique = sorted(seen.values(), key=lambda x: x["month_end"])

    print(f"\nUnique records: {len(unique)}")
    for r in unique[-5:]:
        print(f"  {r['month_end']}: oz/share={r['oz_per_share']}, shares={r['shares']:,}")

    # 写库
    store = DataStore(str(DUCKDB_EXTERNAL))
    inserted = 0
    for r in unique:
        total_oz = r["oz_per_share"] * r["shares"]
        total_tonnes = total_oz / 32150.7
        store.insert_etf_holding(
            symbol="GLD",
            date=r["month_end"],
            total_tonnes=round(total_tonnes, 2),
            total_shares=r["shares"],
            aum_usd=None,
            release_at=etf_release_at(r.get("filing_date"), r["month_end"]),
            source="sec_edgar",
        )
        inserted += 1
    print(f"\nInserted {inserted} rows into etf_holdings")
    latest = unique[-1] if unique else {}
    return {
        "rows": inserted,
        "latest_date": latest.get("month_end"),
        "latest_release_at": etf_release_at(latest.get("filing_date"), latest.get("month_end")) if latest else None,
    }

if __name__ == "__main__":
    main()
