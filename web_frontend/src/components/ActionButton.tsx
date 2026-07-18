import { FormEvent, useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, Eye, EyeOff, LucideIcon, X } from "lucide-react";
import { getApiErrorCode, isStepUpRequiredError, stepUpAuth } from "@/api/client";

type ActionButtonProps = {
  icon: LucideIcon;
  label: string;
  variant: "primary" | "danger" | "ghost";
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  confirmTitle?: string;
  confirmMessage?: string;
  stepUpOnDemand?: boolean;
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
  stepUpOnDemand = false,
  onAction,
}: ActionButtonProps) {
  const [confirming, setConfirming] = useState(false);
  const [stepUpRequired, setStepUpRequired] = useState(false);
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [showStepUpPassword, setShowStepUpPassword] = useState(false);
  const [stepUpBusy, setStepUpBusy] = useState(false);
  const [stepUpError, setStepUpError] = useState("");
  const dialogTitleId = useId();
  const stepUpPasswordId = useId();
  const stepUpErrorId = useId();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const stepUpPasswordRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!confirming) return;
    if (stepUpRequired) stepUpPasswordRef.current?.focus();
    else confirmRef.current?.focus();
  }, [confirming, stepUpRequired]);

  const closeDialog = () => {
    setConfirming(false);
    setStepUpRequired(false);
    setStepUpPassword("");
    setShowStepUpPassword(false);
    setStepUpError("");
  };

  const runConfirmedAction = async () => {
    try {
      await onAction();
      closeDialog();
    } catch (actionError) {
      if (stepUpOnDemand && isStepUpRequiredError(actionError)) {
        setStepUpRequired(true);
        setStepUpPassword("");
        setStepUpError("");
        return;
      }
      closeDialog();
    }
  };

  const submitStepUp = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setStepUpError("");
    setStepUpBusy(true);
    try {
      await stepUpAuth(stepUpPassword);
    } catch (stepUpFailure) {
      const code = getApiErrorCode(stepUpFailure);
      if (code === "invalid_step_up_credentials") {
        setStepUpError("密码不正确，请重试。");
      } else if (code === "step_up_session_unavailable") {
        setStepUpError("会话事实暂不可用，新增风险保持阻断，请稍后重试。");
      } else {
        setStepUpError("再认证失败，请检查会话后重试。");
      }
      setStepUpBusy(false);
      return;
    }

    try {
      await onAction();
    } catch {
      // The owning mutation/page renders the action failure outside the modal.
    } finally {
      setStepUpBusy(false);
      closeDialog();
    }
  };

  return (
    <div className="action-wrap">
      <button
        className={`action-btn action-${variant}`}
        type="button"
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        aria-label={loading ? `${label}请求中` : label}
        onClick={() => {
          setStepUpRequired(false);
          setStepUpError("");
          setConfirming(true);
        }}
      >
        <Icon size={16} aria-hidden="true" />
        <span>{loading ? "请求中…" : label}</span>
      </button>
      {error ? <div className="small error-text action-error" role="alert">{error}</div> : null}
      {confirming ? (
        <div
          className="confirm-backdrop"
          role="presentation"
          onMouseDown={(event) => { if (!stepUpBusy && event.target === event.currentTarget) closeDialog(); }}
          onKeyDown={(event) => { if (!stepUpBusy && event.key === "Escape") closeDialog(); }}
        >
          <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby={dialogTitleId}>
            <button className="confirm-close" type="button" onClick={closeDialog} aria-label="关闭确认窗口" disabled={stepUpBusy}><X size={18} /></button>
            <span className={`confirm-icon ${variant === "danger" ? "confirm-icon-danger" : ""}`} aria-hidden="true"><AlertTriangle size={22} /></span>
            <div className="confirm-copy">
              <h2 id={dialogTitleId}>{stepUpRequired ? `再次验证后${label}` : (confirmTitle || `确认${label}`)}</h2>
              <p>{stepUpRequired ? "当前认证已超过 5 分钟。验证密码后将自动重试本次操作。" : (confirmMessage || `请确认是否继续执行“${label}”。操作结果以服务端响应为准。`)}</p>
            </div>
            {stepUpRequired ? (
              <form className="step-up-form" onSubmit={submitStepUp}>
                <label htmlFor={stepUpPasswordId}>操作员密码</label>
                <span className="password-field">
                  <input
                    ref={stepUpPasswordRef}
                    id={stepUpPasswordId}
                    type={showStepUpPassword ? "text" : "password"}
                    value={stepUpPassword}
                    onChange={(event) => setStepUpPassword(event.target.value)}
                    autoComplete="current-password"
                    aria-describedby={stepUpError ? stepUpErrorId : undefined}
                    disabled={stepUpBusy}
                    required
                  />
                  <button type="button" onClick={() => setShowStepUpPassword((value) => !value)} aria-label={showStepUpPassword ? "隐藏密码" : "显示密码"}>
                    {showStepUpPassword ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
                  </button>
                </span>
                {stepUpError ? <p id={stepUpErrorId} className="error-text" role="alert">{stepUpError}</p> : null}
                <div className="confirm-actions">
                  <button className="action-btn action-ghost" type="button" onClick={closeDialog} disabled={stepUpBusy}>取消</button>
                  <button className={`action-btn action-${variant}`} type="submit" disabled={stepUpBusy || !stepUpPassword} aria-busy={stepUpBusy || undefined}>
                    {stepUpBusy ? "验证中…" : `验证并${label}`}
                  </button>
                </div>
              </form>
            ) : (
              <div className="confirm-actions">
                <button className="action-btn action-ghost" type="button" onClick={closeDialog}>取消</button>
                <button
                  ref={confirmRef}
                  className={`action-btn action-${variant}`}
                  type="button"
                  disabled={loading}
                  onClick={() => void runConfirmedAction()}
                >
                  {loading ? "请求中…" : `确认${label}`}
                </button>
              </div>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
