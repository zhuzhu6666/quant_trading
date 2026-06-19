import { classNames } from "@/lib/format";

interface CardProps {
  children: React.ReactNode;
  title?: string;
  subtitle?: string;
  className?: string;
  padding?: "sm" | "md" | "lg";
  hover?: boolean;
  onClick?: () => void;
  variant?: "default" | "glass" | "flat";
  style?: React.CSSProperties;
}

export function Card({
  children,
  title,
  subtitle,
  className,
  padding = "md",
  hover,
  onClick,
  variant = "default",
  style,
}: CardProps) {
  const Comp = onClick ? "button" : "div";

  const variantStyles = {
    default: "bg-white shadow-card",
    glass: "glass-card",
    flat: "bg-white shadow-apple-sm",
  };

  const paddingStyles = {
    sm: "p-4",
    md: "p-5",
    lg: "p-6",
  };

  return (
    <Comp
      onClick={onClick}
      className={classNames(
        "rounded-3xl text-left text-text-primary",
        variantStyles[variant],
        hover && "cursor-pointer",
        paddingStyles[padding],
        className
      )}
      style={style}
    >
      {title && (
        <div className="flex items-center justify-between mb-4">
          <span className="section-label">{title}</span>
          {subtitle && <span className="text-2xs text-text-secondary">{subtitle}</span>}
        </div>
      )}
      {children}
    </Comp>
  );
}
