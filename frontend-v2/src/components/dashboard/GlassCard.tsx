interface GlassCardProps {
  children: React.ReactNode;
  className?: string;
  hover?: boolean;
  onClick?: () => void;
  style?: React.CSSProperties;
}

export function GlassCard({ children, className = "", hover, onClick, style }: GlassCardProps) {
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      onClick={onClick}
      className={`glass-card ${hover ? "hover:bg-white/88" : ""} ${onClick ? "cursor-pointer text-left w-full" : ""} ${className}`}
      style={style}
    >
      {children}
    </Comp>
  );
}
