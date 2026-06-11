import { classNames } from "@/lib/format";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "warning" | "success";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  icon?: React.ReactNode;
}

const variantStyles: Record<string, string> = {
  primary: "bg-accent hover:bg-accent-hover text-white",
  secondary: "bg-[#e4e9f0] hover:bg-[#dce0e6] text-[#1a1e24] border border-[#dce0e6]",
  ghost: "bg-transparent hover:bg-[#e4e9f0] text-[#6e7681]",
  danger: "bg-down hover:bg-red-600 text-white",
  warning: "bg-warn hover:bg-amber-600 text-white",
  success: "bg-[#3fb950] hover:bg-[#2ea043] text-white",
};

const sizeStyles: Record<string, string> = {
  sm: "px-2 py-1 text-xs",
  md: "px-3 py-1.5 text-sm",
  lg: "px-4 py-2 text-sm",
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  icon,
  className,
  children,
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      className={classNames(
        "inline-flex items-center justify-center gap-1.5 font-medium rounded transition-all duration-150",
        "active:scale-[0.97] disabled:opacity-50 disabled:pointer-events-none",
        variantStyles[variant],
        sizeStyles[size],
        className
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? (
        <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="2" strokeOpacity="0.3" />
          <path d="M15 8a7 7 0 01-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      ) : icon ? (
        <span className="w-3.5 h-3.5">{icon}</span>
      ) : null}
      {children}
    </button>
  );
}
