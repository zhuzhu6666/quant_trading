import { classNames } from "@/lib/format";

interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  options: Array<{ value: string; label: string }>;
}

export function Select({ label, options, className, ...props }: SelectProps) {
  return (
    <div className="flex flex-col gap-1">
      {label && <label className="text-xs text-fg-muted font-medium">{label}</label>}
      <select
        className={classNames(
          "w-full bg-white border border-[#dce0e6] rounded px-2.5 py-1.5 text-sm text-[#1a1e24]",
          "focus:border-accent focus:ring-1 focus:ring-accent/30 outline-none transition-colors duration-150",
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
