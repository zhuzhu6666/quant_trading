import { classNames } from "@/lib/format";

interface BadgeProps {
  variant?: "default" | "success" | "warning" | "danger" | "info" | "gold";
  children: React.ReactNode;
  className?: string;
}

const variantStyles: Record<string, string> = {
  default: "bg-[#e4e9f0] text-[#1a1e24]",
  success: "bg-up-muted text-up",
  warning: "bg-warn-muted text-warn",
  danger: "bg-down-muted text-down",
  info: "bg-accent-muted text-accent",
  gold: "bg-primary-muted text-primary",
};

export function Badge({ variant = "default", children, className }: BadgeProps) {
  return (
    <span
      className={classNames(
        "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium",
        variantStyles[variant],
        className
      )}
    >
      {children}
    </span>
  );
}
