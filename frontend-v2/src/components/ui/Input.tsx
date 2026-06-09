import { classNames } from "@/lib/format";

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  monospace?: boolean;
}

export function Input({ label, error, monospace, className, ...props }: InputProps) {
  return (
    <div className="flex flex-col gap-1">
      {label && <label className="text-xs text-fg-muted font-medium">{label}</label>}
      <input
        className={classNames(
          "w-full bg-[#0d1117] border border-border rounded px-2.5 py-1.5 text-sm text-[#e6edf3] placeholder:text-fg-placeholder",
          "focus:border-accent focus:ring-1 focus:ring-accent/30 outline-none transition-colors duration-150",
          monospace && "num",
          error && "border-down",
          className
        )}
        {...props}
      />
      {error && <span className="text-xs text-down">{error}</span>}
    </div>
  );
}
