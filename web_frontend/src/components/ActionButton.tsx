import { useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, LucideIcon, X } from "lucide-react";

type ActionButtonProps = {
  icon: LucideIcon;
  label: string;
  variant: "primary" | "danger" | "ghost";
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  confirmTitle?: string;
  confirmMessage?: string;
  onAction: () => Promise<unknown> | unknown;
};

export function ActionButton({
  icon: Icon,
  label,
  variant,
  disabled,
  loading,
  error,
  confirmTitle,
  confirmMessage,
  onAction,
}: ActionButtonProps) {
  const [confirming, setConfirming] = useState(false);
  const dialogTitleId = useId();
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  return (
    <div className="action-wrap">
      <button
        className={`action-btn action-${variant}`}
        type="button"
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        aria-label={loading ? `${label}请求中` : label}
        onClick={() => setConfirming(true)}
      >
        <Icon size={16} aria-hidden="true" />
        <span>{loading ? "请求中…" : label}</span>
      </button>
      {error ? <div className="small error-text action-error" role="alert">{error}</div> : null}
      {confirming ? (
        <div
          className="confirm-backdrop"
          role="presentation"
          onMouseDown={(event) => { if (event.target === event.currentTarget) setConfirming(false); }}
          onKeyDown={(event) => { if (event.key === "Escape") setConfirming(false); }}
        >
          <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby={dialogTitleId}>
            <button className="confirm-close" type="button" onClick={() => setConfirming(false)} aria-label="关闭确认窗口"><X size={18} /></button>
            <span className={`confirm-icon ${variant === "danger" ? "confirm-icon-danger" : ""}`} aria-hidden="true"><AlertTriangle size={22} /></span>
            <div className="confirm-copy">
              <h2 id={dialogTitleId}>{confirmTitle || `确认${label}`}</h2>
              <p>{confirmMessage || `请确认是否继续执行“${label}”。操作结果以服务端响应为准。`}</p>
            </div>
            <div className="confirm-actions">
              <button className="action-btn action-ghost" type="button" onClick={() => setConfirming(false)}>取消</button>
              <button
                ref={confirmRef}
                className={`action-btn action-${variant}`}
                type="button"
                onClick={async () => { try { await onAction(); } finally { setConfirming(false); } }}
              >
                确认{label}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
