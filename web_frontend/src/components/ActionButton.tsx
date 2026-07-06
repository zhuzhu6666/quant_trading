import { useState } from "react";
import { LucideIcon } from "lucide-react";

type ActionButtonProps = {
  icon: LucideIcon;
  label: string;
  variant: "primary" | "danger" | "ghost";
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  onAction: () => Promise<void> | void;
};

export function ActionButton({
  icon: Icon,
  label,
  variant,
  disabled,
  loading,
  error,
  onAction,
}: ActionButtonProps) {
  const [confirming, setConfirming] = useState(false);
  const buttonLabel = loading ? `${label}请求中` : confirming ? `再次确认${label}` : label;

  return (
    <div className="action-wrap">
      <button
        className={`action-btn action-${variant}`}
        type="button"
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        aria-label={buttonLabel}
        data-confirming={confirming || undefined}
        onClick={async () => {
          if (!confirming) {
            setConfirming(true);
            return;
          }
          try {
            await onAction();
            setConfirming(false);
          } catch {
            setConfirming(false);
          }
        }}
      >
        <Icon size={16} />
        <span>
          {loading
            ? "请求中..."
            : confirming
              ? `再次确认 ${label}`
              : label}
        </span>
      </button>
      {error ? <div className="small error-text action-error" role="alert">{error}</div> : null}
      {confirming ? (
        <button
          className="action-cancel"
          type="button"
          aria-label={`取消${label}确认`}
          onClick={() => setConfirming(false)}
        >
          取消
        </button>
      ) : null}
    </div>
  );
}
