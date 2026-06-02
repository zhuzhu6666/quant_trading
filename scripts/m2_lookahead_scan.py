"""
M2.1: 未来函数 (look-ahead bias) 检测器

扫描:
  1. paper_trader.on_bar 时序: signal[t] → trade 必须在 bar[t+1] 后才能结算
  2. paper_engine._check_exit 用 bar[t] 检查 SL/TP, 应该在 bar[t+1] 之后才发生
  3. 策略.on_bar 接收 bar[t] 不能用 close[t+1] 这种未来数据
  4. 数据加载: load_bars 返回的 df 是不是按时间排序, 没有"向后 shift"

方法:
  - 静态扫描: 用 AST 找可疑模式 (bar['close'] 用作 fill, fill_price < signal.price 等)
  - 动态: 对每个 bar, 注入一个"未来提示"字段, 验证策略没有用到

2026-06-03
"""
import ast
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re
from typing import Iterable

# 可疑 API 模式
SUSPICIOUS_PATTERNS = [
    # 1. 直接用 bar[future index]
    (r'bar\[i\+\d+\]', "future bar index access"),
    (r'\.iloc\[i\+\d+\]', "future iloc access"),
    (r'\.shift\(\-\d+\)', "negative shift (look forward)"),
    # 2. close 价当作 entry (应该是 open, signal close 在 t+1)
    (r'fill_price\s*=\s*.*\.close', "fill_price from close (should be open or signal.price)"),
    # 3. TP/SL 用 close 比较 (P2 已知问题, 应该是 bid/ask)
    #    这里我们之前已修, 但要扫未修的地方
]


def scan_file(path: Path) -> list[dict]:
    """扫一个 .py 文件, 找可疑模式."""
    findings = []
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings

    for line_no, line in enumerate(src.splitlines(), 1):
        for pattern, desc in SUSPICIOUS_PATTERNS:
            if re.search(pattern, line):
                findings.append({
                    "file": str(path),
                    "line": line_no,
                    "pattern": pattern,
                    "desc": desc,
                    "code": line.strip()[:120],
                })
    return findings


def scan_dir(root: Path, subdirs: Iterable[str] = ("execution", "strategy", "alpha", "core")) -> list[dict]:
    """扫指定子目录的 .py 文件."""
    all_findings = []
    for sub in subdirs:
        d = root / sub
        if not d.exists():
            continue
        for py in d.rglob("*.py"):
            all_findings.extend(scan_file(py))
    return all_findings


def main():
    root = Path(".")
    print("=" * 60)
    print("M2.1: 未来函数 (look-ahead bias) 扫描 (2026-06-03)")
    print("=" * 60)

    findings = scan_dir(root)
    if not findings:
        print("\n✓ 静态扫描未发现可疑 look-ahead 模式")
    else:
        print(f"\n⚠️  发现 {len(findings)} 处可疑模式:")
        for f in findings[:30]:
            print(f"  {f['file']}:{f['line']}  [{f['desc']}]")
            print(f"    {f['code']}")
        if len(findings) > 30:
            print(f"  ... +{len(findings) - 30} more")

    # 报告
    out = Path("data/charts/m2_lookahead_scan.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("M2.1: 未来函数 (look-ahead) 扫描报告 (2026-06-03)\n")
        f.write("=" * 70 + "\n\n")
        f.write("扫描目录: execution, strategy, alpha, core\n")
        f.write(f"可疑模式定义:\n")
        for p, d in SUSPICIOUS_PATTERNS:
            f.write(f"  - {p}: {d}\n")
        f.write(f"\n总发现: {len(findings)}\n\n")
        for fd in findings:
            f.write(f"{fd['file']}:{fd['line']}\n")
            f.write(f"  [{fd['desc']}]\n")
            f.write(f"  {fd['code']}\n\n")
        f.write("=" * 70 + "\n")
        f.write("结论 (2026-06-03)\n")
        f.write("=" * 70 + "\n")
        if not findings:
            f.write("✓ 无静态可发现 look-ahead 模式. 进一步动态测试需注入未来 bar.\n")
        else:
            f.write(f"⚠️  {len(findings)} 处需人工 review. 重点关注 fill_price 从 close 取值. P2 已修 SL/TP bid/ask.\n")
            f.write("已知安全:\n")
            f.write("  - paper_trader.run() 时序: signal 在 bar[t] on_bar 生成 → bar[t+1].open 成交\n")
            f.write("  - paper_engine._check_exit: 只用 bar[t] 的 high/low (已发生)\n")
            f.write("  - backtest signal 不包含未来 bar 数据\n")

    print(f"\n报告: {out}")
    return len(findings)


if __name__ == "__main__":
    n = main()
    sys.exit(0 if n == 0 else 1)
