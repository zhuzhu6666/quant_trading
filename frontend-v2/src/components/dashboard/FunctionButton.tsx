interface FunctionButtonProps {
  icon: string;
  label: string;
  description: string;
  accent: string;
  onClick: () => void;
}

export function FunctionButton({ icon, label, description, accent, onClick }: FunctionButtonProps) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center justify-center p-4 rounded-2xl cursor-pointer border-none transition-all duration-300 ease-apple
        hover:-translate-y-[3px]"
      style={{
        background: "#FFFFFF",
        boxShadow: "0 0 0 1px rgba(0,0,0,0.04), 0 2px 12px rgba(0,0,0,0.06)",
        "--accent": accent,
      } as React.CSSProperties}
      onMouseEnter={(e) => {
        e.currentTarget.style.boxShadow = `0 0 0 1px ${accent}33, 0 8px 24px ${accent}26`;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.boxShadow = "0 0 0 1px rgba(0,0,0,0.04), 0 2px 12px rgba(0,0,0,0.06)";
      }}
    >
      <span className="text-2xl mb-2 transition-transform duration-300 group-hover:scale-110">{icon}</span>
      <span className="text-sm font-semibold text-text-primary">{label}</span>
      <span className="text-2xs text-text-secondary mt-0.5">{description}</span>
    </button>
  );
}
