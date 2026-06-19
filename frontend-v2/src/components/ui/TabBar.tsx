interface TabBarProps {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (key: string) => void;
}

export function TabBar({ tabs, active, onChange }: TabBarProps) {
  return (
    <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: "rgba(255,255,255,0.5)" }}>
      {tabs.map((t) => (
        <button
          key={t.key}
          onClick={() => onChange(t.key)}
          className={`flex-1 py-1.5 px-3 rounded-md text-xs font-medium transition-all duration-200 ${
            active === t.key
              ? "bg-[#d4edda] text-[#1a1e24]"
              : "bg-[#dce0e6] text-[#4a4f59] hover:bg-[#d0d5dd] hover:text-[#1a1e24]"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
