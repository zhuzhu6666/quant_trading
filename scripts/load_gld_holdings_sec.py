"""从 SEC EDGAR 拉 GLD/SLV ETF 持仓（动态查询 CIK）。"""
import re, sys, time, json, subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.db import DUCKDB_EXTERNAL
from data.external_schema import etf_release_at, record_raw_file
from data.store import DataStore

FUND_CONFIG = (
    ("GLD", "1222333", Path("data/sec_gld")),
    ("SLV", "1330568", Path("data/sec_slv")),
)
MAX_FILINGS_PER_FUND = 12  # recent 3 years is enough for live factors and keeps SEC jobs bounded
# Kept as a compatibility alias for callers/tests that inspect the old GLD cache.
CACHE_DIR = FUND_CONFIG[0][2]
CACHE_DIR.mkdir(parents=True, exist_ok=True)
UA = "ZhuQuant zhu@zhuzhu666.icu"
CURL = ["curl", "-sL", "--max-time", "60", "--proxy", "http://127.0.0.1:7890", "-H", f"User-Agent: {UA}"]

def get_filings(cik: str):
    """从 SEC submissions API 获取指定 ETF 的 10-Q/10-K 列表。"""
    url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
    r = subprocess.run(CURL + [url], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        print(f"SEC API failed: {r.stderr[:200]}")
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        print(f"SEC API returned invalid JSON: {exc}")
        return []
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

def get_gld_filings():
    """兼容旧调用方的 GLD filing 查询。"""
    return get_filings("1222333")


def download_filing(
    accn: str,
    accn_clean: str,
    doc: str,
    cik: str = "1222333",
    cache_dir: Path | None = None,
) -> Path | None:
    """下载 filing 文档"""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / doc
    if out.exists() and out.stat().st_size > 100000:
        return out
    url = f"https://www.sec.gov/Archives/edgar/data/{str(cik).lstrip('0')}/{accn_clean}/{doc}"
    print(f"  downloading: {doc} ...")
    for attempt in range(3):
        r = subprocess.run(CURL + ["-o", str(out), url], capture_output=True, timeout=40)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 50000:
            time.sleep(0.5)
            return out
        print(f"    attempt {attempt+1} failed, size={out.stat().st_size if out.exists() else 0}")
        time.sleep(3)
    return None


def filing_url(accn_clean: str, doc: str, cik: str = "1222333") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{str(cik).lstrip('0')}/{accn_clean}/{doc}"

def parse_10q(path: Path, symbol: str = "GLD") -> list[dict]:
    """从 10-Q/10-K 提取 GLD 月度或 SLV 季度持仓。"""
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
    if results or symbol.upper() != "SLV":
        return results

    # SLV filings disclose total silver ounces at quarter ends rather than
    # GLD's monthly ounces-per-share table.  Preserve the same downstream
    # record shape and carry total_oz explicitly so it is not multiplied by
    # shares a second time.
    schedule_match = re.search(
        r"Schedules of Investments.*?At\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+and\s+([A-Z][a-z]+ \d{1,2}, \d{4})",
        clean,
        flags=re.I,
    )
    date_strings = list(schedule_match.groups()) if schedule_match else re.findall(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", clean)
    dates = []
    for raw_date in date_strings:
        try:
            parsed_date = datetime.strptime(raw_date, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if parsed_date not in dates:
            dates.append(parsed_date)
    ounce_values = [int(value.replace(",", "")) for value in re.findall(r"Silver bullion\s+([\d,]+)\s+\$", clean, flags=re.I)]
    shares_match = re.search(r"Shares issued and outstanding[^\d]+([\d,]+)\s+([\d,]+)", clean, flags=re.I)
    share_values = [int(value.replace(",", "")) for value in shares_match.groups()] if shares_match else []
    for index, total_oz in enumerate(ounce_values[:2]):
        if index >= len(dates):
            break
        results.append(
            {
                "month_end": dates[index],
                "oz_per_share": 0.0,
                "total_oz": total_oz,
                "shares": share_values[index] if index < len(share_values) else 0,
            }
        )
    return results

def _load_fund(symbol: str, cik: str, cache_dir: Path) -> dict:
    filings = get_filings(cik)
    filings = filings[:MAX_FILINGS_PER_FUND]
    print(f"[{symbol}] Found {len(filings)} 10-Q/10-K filings")
    all_monthly: list[dict] = []
    failures: list[str] = []
    for filing in filings:
        path = download_filing(
            filing["accession"],
            filing["accession_clean"],
            filing["document"],
            cik=cik,
            cache_dir=cache_dir,
        )
        if not path:
            failures.append(f"{filing['filing_date']} {filing['document']}: download failed")
            continue
        try:
            records = parse_10q(path, symbol=symbol)
        except Exception as exc:
            failures.append(f"{filing['filing_date']} {filing['document']}: parse failed: {exc}")
            continue
        source_url = filing_url(filing["accession_clean"], filing["document"], cik)
        for record in records:
            record["symbol"] = symbol
            record["filing_date"] = filing["filing_date"]
            record["source_url"] = source_url
        record_raw_file(f"sec_{symbol.lower()}", path, source_url=source_url)
        print(f"  {filing['filing_date']} {filing['document']}: {len(records)} monthly records, size={path.stat().st_size//1024}KB")
        all_monthly.extend(records)
        time.sleep(1)

    seen: dict[str, dict] = {}
    for record in all_monthly:
        key = record["month_end"]
        # Prefer the newest filing when a later filing restates a month.
        if key not in seen or str(record.get("filing_date") or "") >= str(seen[key].get("filing_date") or ""):
            seen[key] = record
    unique = sorted(seen.values(), key=lambda item: item["month_end"])
    if not unique:
        return {"symbol": symbol, "rows": 0, "latest_date": None, "latest_release_at": None, "errors": failures or ["no data extracted"]}

    store = DataStore(str(DUCKDB_EXTERNAL))
    inserted = 0
    for record in unique:
        total_oz = record.get("total_oz") or (record["oz_per_share"] * record["shares"])
        store.insert_etf_holding(
            symbol=symbol,
            date=record["month_end"],
            total_tonnes=round(total_oz / 32150.7, 2),
            total_shares=record["shares"],
            aum_usd=None,
            release_at=etf_release_at(record.get("filing_date"), record["month_end"]),
            source="sec_edgar",
        )
        inserted += 1
    latest = unique[-1]
    print(f"[{symbol}] Inserted {inserted} rows into etf_holdings")
    return {
        "symbol": symbol,
        "rows": inserted,
        "latest_date": latest.get("month_end"),
        "latest_release_at": etf_release_at(latest.get("filing_date"), latest.get("month_end")),
        "errors": failures,
    }


def main():
    print("=" * 60)
    print(" GLD/SLV ETF Holdings Loader v4 (SEC API dynamic)")
    print("=" * 60)

    fund_results = []
    for symbol, cik, cache_dir in FUND_CONFIG:
        try:
            fund_results.append(_load_fund(symbol, cik, cache_dir))
        except Exception as exc:
            print(f"[{symbol}] loader failed: {exc}")
            fund_results.append({"symbol": symbol, "rows": 0, "errors": [str(exc)]})

    successful = [result for result in fund_results if int(result.get("rows", 0) or 0) > 0]
    total_rows = sum(int(result.get("rows", 0) or 0) for result in fund_results)
    errors = [f"{result.get('symbol')}: {error}" for result in fund_results for error in result.get("errors", [])]
    latest_result = max(successful, key=lambda result: str(result.get("latest_date") or ""), default={})
    status = "success" if successful and not errors else ("partial" if successful else "failed")
    return {
        "ok": bool(successful),
        "status": status,
        "rows": total_rows,
        "latest_date": latest_result.get("latest_date"),
        "latest_release_at": latest_result.get("latest_release_at"),
        "symbols": [result.get("symbol") for result in successful],
        "errors": errors,
        "error": "; ".join(errors)[:500] if errors else None,
    }

if __name__ == "__main__":
    main()
