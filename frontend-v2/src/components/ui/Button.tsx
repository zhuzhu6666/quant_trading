import { classNames } from "@/lib/format";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "warning" | "success";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  icon?: React.ReactNode;
}

const variantStyles: Record<string, string> = {
  primary: "btn-primary",
  secondary: "btn-secondary",
  ghost: "btn-ghost",
  danger: "bg-danger text-white hover:bg-red-500 active:bg-red-600 shadow-apple-sm",
  warning: "bg-warning text-white hover:bg-amber-500 active:bg-amber-600 shadow-apple-sm",
  success: "bg-success text-white hover:bg-green-500 active:bg-green-600 shadow-apple-sm",
};

const sizeStyles: Record<string, string> = {
  sm: "px-3 py-1.5 text-xs",
  md: "px-4 py-2 text-sm",
  lg: "px-5 py-2.5 text-sm",
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
        "inline-flex items-center justify-center gap-1.5 font-medium rounded-xl",
        "active:scale-[0.97] disabled:opacity-40 disabled:pointer-events-none",
        "transition-all duration-200 ease-apple",
        variantStyles[variant],
        sizeStyles[size],
        className
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? (
        <svg className="animate-spin h-4 w-4" viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="2" strokeOpacity="0.3" />
          <path d="M15 8a7 7 0 01-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      ) : icon ? (
        <span className="w-4 h-4 flex items-center justify-center">{icon}</span>
      ) : null}
      {children}
    </button>
  );
}
