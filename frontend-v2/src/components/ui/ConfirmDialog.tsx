import { Modal } from "./Modal";
import { Button } from "./Button";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  variant?: "danger" | "warning" | "primary";
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

const variantButton: Record<string, "danger" | "warning" | "primary"> = {
  danger: "danger",
  warning: "warning",
  primary: "primary",
};

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "确认",
  variant = "primary",
  onConfirm,
  onCancel,
  loading,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      actions={
        <>
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={loading}>
            取消
          </Button>
          <Button variant={variantButton[variant]} size="sm" onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-sm text-text-secondary">{message}</p>
    </Modal>
  );
}
