interface FunctionButtonProps {
  icon: string;
  label: string;
  description: string;
  gradient: "blue" | "green" | "amber" | "purple" | "slate";
  onClick: () => void;
}

const GRADIENTS = {
  blue: "linear-gradient(135deg, #3b82f6, #1d4ed8)",
  green: "linear-gradient(135deg, #16a34a, #15803d)",
  amber: "linear-gradient(135deg, #d97706, #b45309)",
  purple: "linear-gradient(135deg, #7c3aed, #5b21b6)",
  slate: "linear-gradient(135deg, #475569, #334155)",
};

const SHADOWS = {
  blue: "0 4px 16px rgba(59,130,246,0.25)",
  green: "0 4px 16px rgba(22,163,74,0.25)",
  amber: "0 4px 16px rgba(217,119,6,0.25)",
  purple: "0 4px 16px rgba(124,58,237,0.25)",
  slate: "0 4px 16px rgba(71,85,105,0.25)",
};

export function FunctionButton({ icon, label, description, gradient, onClick }: FunctionButtonProps) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center justify-center p-3 rounded-xl cursor-pointer border-none transition-all duration-400"
      style={{
        background: GRADIENTS[gradient],
        boxShadow: SHADOWS[gradient],
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = "translateY(-4px) scale(1.03)";
        e.currentTarget.style.boxShadow = "0 12px 32px rgba(0,0,0,0.3)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "none";
        e.currentTarget.style.boxShadow = SHADOWS[gradient];
      }}
    >
      <span className="text-xl mb-1">{icon}</span>
      <span className="text-xs font-semibold text-white">{label}</span>
      <span className="text-[10px] text-white/70 mt-0.5">{description}</span>
    </button>
  );
}
