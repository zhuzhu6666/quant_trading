import { classNames } from "@/lib/format";

interface SkeletonProps {
  variant?: "text" | "card" | "table-row" | "metric";
  count?: number;
  className?: string;
}

const variantStyles: Record<string, string> = {
  text: "h-4 bg-[#e4e9f0] rounded w-full",
  card: "h-[100px] bg-white border border-[#dce0e6] rounded-lg",
  "table-row": "h-10 bg-[#e4e9f0]/50 rounded w-full",
  metric: "h-16 bg-white border border-[#dce0e6] rounded-lg",
};

export function Skeleton({ variant = "text", count = 1, className }: SkeletonProps) {
  const items = Array.from({ length: count }, (_, i) => i);
  if (count === 1) {
    return <div className={classNames(variantStyles[variant], "animate-pulse", className)} />;
  }
  return (
    <div className="flex flex-col gap-3">
      {items.map((i) => (
        <div key={i} className={classNames(variantStyles[variant], "animate-pulse", className)} />
      ))}
    </div>
  );
}
