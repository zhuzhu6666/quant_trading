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

  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  useEffect(() => {
    if (open) {
      setVisible(true);
      requestAnimationFrame(() => setAnimating(true));
    } else if (visibleRef.current) {
      setAnimating(false);
      const timer = setTimeout(() => setVisible(false), 300);
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

  const handleBackdropPointerDown = (e: React.PointerEvent) => {
    // Clicks on the backdrop (not panel content, which has stopPropagation) close the panel
    e.preventDefault();
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50" onPointerDown={handleBackdropPointerDown}>
      <div
        className="absolute inset-0"
        style={{
          background: "rgba(0,0,0,0.15)",
          opacity: animating ? 1 : 0,
          transition: "opacity 0.3s",
        }}
      />
      <div
        ref={panelRef}
        onPointerDown={(e) => e.stopPropagation()}
        className={`fixed top-0 left-0 right-0 max-h-[85vh] overflow-y-auto rounded-b-3xl shadow-xl
          ${className}`}
        style={{
          background: "rgba(255,255,255,0.88)",
          backdropFilter: "blur(24px) saturate(180%)",
          WebkitBackdropFilter: "blur(24px) saturate(180%)",
          borderBottom: "1px solid rgba(255,255,255,0.5)",
          transform: animating ? "translateY(0)" : "translateY(-100%)",
          transition: "transform 0.35s cubic-bezier(0.32, 0.72, 0, 1)",
        }}
      >
        <div
          className="sticky top-0 z-10 flex items-center justify-between px-6 py-3.5 border-b border-apple-divider"
          style={{
            background: "rgba(255,255,255,0.6)",
            backdropFilter: "blur(12px)",
          }}
        >
          <h2 className="text-sm font-semibold text-text-primary">{title}</h2>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onClose(); }}
            className="w-8 h-8 flex items-center justify-center rounded-full hover:bg-black/5 transition-colors text-text-secondary hover:text-text-primary"
          >
            <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
              <path d="M4.646 4.646a.5.5 0 01.708 0L8 7.293l2.646-2.647a.5.5 0 01.708.708L8.707 8l2.647 2.646a.5.5 0 01-.708.708L8 8.707l-2.646 2.647a.5.5 0 01-.708-.708L7.293 8 4.646 5.354a.5.5 0 010-.708z" />
            </svg>
          </button>
        </div>
        <div className="p-6">
          {children}
        </div>
      </div>
    </div>
  );
}
