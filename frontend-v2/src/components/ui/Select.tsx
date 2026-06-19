import { classNames } from "@/lib/format";

interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  options: Array<{ value: string; label: string }>;
}

export function Select({ label, options, className, ...props }: SelectProps) {
  return (
    <div className="flex flex-col gap-1.5">
      {label && <label className="text-xs text-text-secondary font-medium">{label}</label>}
      <select
        className={classNames(
          "w-full bg-apple-bg border border-apple-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary",
          "focus:border-accent focus:ring-2 focus:ring-accent/20 outline-none transition-all duration-200",
          className
        )}
        {...props}
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}
