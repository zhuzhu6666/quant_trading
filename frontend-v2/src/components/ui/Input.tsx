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
          "w-full bg-white border border-[#dce0e6] rounded px-2.5 py-1.5 text-sm text-[#1a1e24] placeholder:text-[#9ea4ae]",
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
