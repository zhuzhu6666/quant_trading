import { classNames } from "@/lib/format";

interface SkeletonProps {
  variant?: "text" | "card" | "table-row" | "metric";
  count?: number;
  className?: string;
}

const variantStyles: Record<string, string> = {
  text: "h-4 bg-apple-bg rounded-xl w-full",
  card: "h-[100px] bg-white shadow-card rounded-3xl",
  "table-row": "h-10 bg-apple-bg/60 rounded-xl w-full",
  metric: "h-16 bg-white shadow-card rounded-3xl",
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
