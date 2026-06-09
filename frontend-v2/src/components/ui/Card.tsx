import { classNames } from "@/lib/format";

interface CardProps {
  children: React.ReactNode;
  title?: string;
  subtitle?: string;
  className?: string;
  padding?: "sm" | "md";
  hover?: boolean;
  onClick?: () => void;
}

export function Card({ children, title, subtitle, className, padding = "md", hover, onClick }: CardProps) {
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      onClick={onClick}
      className={classNames(
        "bg-[#1c2128] border border-border rounded-lg text-left text-[#e6edf3]",
        hover && "transition-all duration-150 hover:shadow-card-hover hover:-translate-y-0.5",
        padding === "sm" ? "p-3" : "p-4",
        onClick && "cursor-pointer w-full",
        className
      )}
    >
      {title && (
        <div className="flex items-center justify-between mb-3">
          <span className="text-xs text-fg-muted font-medium uppercase tracking-wider">{title}</span>
          {subtitle && <span className="text-xs text-fg-muted">{subtitle}</span>}
        </div>
      )}
      {children}
    </Comp>
  );
}
