"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { classNames } from "@/lib/format";

const ITEMS = [
  { href: "/", label: "总览", icon: "🏠" },
  { href: "/paper", label: "模拟盘", icon: "▶" },
  { href: "/backtest", label: "回测", icon: "▶" },
  { href: "/market", label: "K线", icon: "📈" },
  { href: "/factors", label: "因子", icon: "🧪" },
  { href: "/discover", label: "发现", icon: "🔍" },
  { href: "/tuning", label: "调参", icon: "🎛" },
  { href: "/calibrator", label: "校准", icon: "📐" },
  { href: "/shadow", label: "影子", icon: "👻" },
  { href: "/ab", label: "A/B", icon: "⚖" },
  { href: "/sync", label: "同步", icon: "🔄" },
  { href: "/live", label: "实盘", icon: "💰" },
  { href: "/reports", label: "报告", icon: "📑" },
  { href: "/config", label: "配置", icon: "⚙" },
  { href: "/jobs", label: "任务", icon: "📋" },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <nav className="w-60 bg-bg-card border-r border-bg-border h-screen sticky top-0 p-4 flex flex-col gap-1">
      <div className="text-accent font-bold text-lg mb-4 px-2">⚡ Quant</div>
      {ITEMS.map((item) => {
        const active = pathname === item.href;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={classNames(
              "flex items-center gap-3 px-3 py-2 rounded text-sm",
              active
                ? "bg-accent/10 text-accent border-l-[3px] border-accent"
                : "text-fg-muted hover:bg-bg-border hover:text-fg"
            )}
          >
            <span className="w-5 text-center">{item.icon}</span>
            <span>{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
