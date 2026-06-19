import { classNames } from "@/lib/format";

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  monospace?: boolean;
}

export function Input({ label, error, monospace, className, ...props }: InputProps) {
  return (
    <div className="flex flex-col gap-1.5">
      {label && <label className="text-xs text-text-secondary font-medium">{label}</label>}
      <input
        className={classNames(
          "w-full bg-apple-bg border border-apple-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary placeholder:text-text-tertiary",
          "focus:border-accent focus:ring-2 focus:ring-accent/20 outline-none transition-all duration-200",
          monospace && "num",
          error && "border-danger",
          className
        )}
        {...props}
      />
      {error && <span className="text-xs text-danger">{error}</span>}
    </div>
  );
}
