import { useEffect, useRef, useState } from "react";

interface SlidePanelProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  className?: string;
}

export function SlidePanel({ open, onClose, title, children, className = "" }: SlidePanelProps) {
  const [visible, setVisible] = useState(false);
  const [animating, setAnimating] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // B11 fix: 之前 useEffect deps 只有 [open], 但内部读 `visible` 触发 close 路径, 会 stale closure.
  // 用 visibleRef 跟踪最新值避免重 effect 循环, 同时保持 deps 只 [open].
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  useEffect(() => {
    if (open) {
      setVisible(true);
      requestAnimationFrame(() => setAnimating(true));
    } else if (visibleRef.current) {
      setAnimating(false);
      const timer = setTimeout(() => setVisible(false), 250);
      return () => clearTimeout(timer);
    }
  }, [open]);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open) onClose();
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onClose]);

  if (!visible) return null;

  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      <div className="absolute inset-0 bg-black/10 backdrop-blur-sm" />
      <div
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        className={`fixed top-0 left-0 right-0 max-h-[85vh] overflow-y-auto rounded-b-2xl shadow-2xl
          ${animating ? "panel-slide" : "panel-slide-out"}
          ${className}`}
        style={{ background: "rgba(255,255,255,0.85)", backdropFilter: "blur(24px)", WebkitBackdropFilter: "blur(24px)", borderBottom: "1px solid rgba(255,255,255,0.9)" }}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3 border-b border-white/30" style={{ background: "rgba(255,255,255,0.5)", backdropFilter: "blur(12px)" }}>
          <h2 className="text-sm font-semibold text-fg">{title}</h2>
          <button onClick={onClose} className="w-7 h-7 flex items-center justify-center rounded-full hover:bg-black/5 transition-colors text-fg-muted hover:text-fg">
            <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
              <path d="M4.646 4.646a.5.5 0 01.708 0L8 7.293l2.646-2.647a.5.5 0 01.708.708L8.707 8l2.647 2.646a.5.5 0 01-.708.708L8 8.707l-2.646 2.647a.5.5 0 01-.708-.708L7.293 8 4.646 5.354a.5.5 0 010-.708z" />
            </svg>
          </button>
        </div>
        <div className="p-5">
          {children}
        </div>
      </div>
    </div>
  );
}
