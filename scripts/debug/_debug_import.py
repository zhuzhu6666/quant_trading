"""Debug why import_to_db fails for specific hours."""
import struct, lzma, duckdb, pandas as pd
from datetime import datetime, timezone
from pathlib import Path

PIPET = 0.001
RAW_DIR = Path("data/dukascopy_raw/XAUUSD")

# Try each hour on June 18 to see which ones fail
for h in range(24):
    fn = RAW_DIR / "2025" / "06" / "18" / f"{h:02d}h_ticks.bi5"
    if not fn.exists() or fn.stat().st_size == 0:
        print(f"Jun 18 {h:02d}h: SKIP (no data file)")
        continue
    
    try:
        raw = fn.read_bytes()
        dec = lzma.decompress(raw)
        n = len(dec) // 20
        base = int(datetime(2026, 6, 18, h, tzinfo=timezone.utc).timestamp())
        records = []
        for i in range(n):
            v = struct.unpack('>5i', dec[i*20:(i+1)*20])
            if v[2] <= 0 or v[1] <= 0:
                continue
            records.append(("XAUUSD+", base+v[0]/1000.0, v[2]*PIPET, v[1]*PIPET,
                          (v[2]+v[1])*PIPET/2, v[4]))
        
        if not records:
            print(f"Jun 18 {h:02d}h: 0 records after filter")
            continue
        
        df = pd.DataFrame(records, columns=["symbol","time","bid","ask","last","volume"])
        c = duckdb.connect("data/ticks.duckdb")
        before = c.execute("SELECT COUNT(*) FROM ticks WHERE time >= ? AND time < ?", [base, base+3600]).fetchone()[0]
        c.execute("INSERT OR IGNORE INTO ticks SELECT * FROM df")
        after = c.execute("SELECT COUNT(*) FROM ticks WHERE time >= ? AND time < ?", [base, base+3600]).fetchone()[0]
        c.close()
        new = after - before
        status = f"OK (+{new} new, {len(records)} processed)"
        print(f"Jun 18 {h:02d}h: {status}")
    except Exception as e:
        print(f"Jun 18 {h:02d}h: ERROR: {e}")
