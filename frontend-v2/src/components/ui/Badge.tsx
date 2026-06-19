import { classNames } from "@/lib/format";

interface BadgeProps {
  variant?: "default" | "success" | "warning" | "danger" | "info" | "accent" | "ghost" | "gold";
  children: React.ReactNode;
  className?: string;
  dot?: boolean;
}

const variantStyles: Record<string, string> = {
  default: "bg-apple-surface-raised text-text-secondary",
  success: "bg-success-light text-success",
  warning: "bg-warning-light text-warning",
  danger: "bg-danger-light text-danger",
  info: "bg-info-light text-info",
  accent: "bg-accent-light text-accent",
  ghost: "bg-apple-bg text-text-secondary border border-apple-border",
  gold: "bg-warning-light text-warning",
};

export function Badge({ variant = "default", children, className, dot }: BadgeProps) {
  return (
    <span
      className={classNames(
        "badge",
        variantStyles[variant],
        className
      )}
    >
      {dot && (
        <span
          className={classNames(
            "w-1.5 h-1.5 rounded-full mr-1.5",
            variant === "success" && "bg-success",
            variant === "warning" && "bg-warning",
            variant === "danger" && "bg-danger",
            variant === "info" && "bg-info",
            variant === "accent" && "bg-accent",
            variant === "default" && "bg-text-secondary"
          )}
        />
      )}
      {children}
    </span>
  );
}
