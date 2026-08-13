import { useEffect, useMemo, useState } from "react";
import { Command, Search } from "lucide-react";
import { Dialog, DialogSurface, DialogTitle } from "@/design-system/primitives";
import { uiStatus } from "@/i18n/zh-CN";
import type { WorkbenchCommand } from "@/shell/commands";

export function CommandPalette({ open, onOpenChange, commands }: { open: boolean; onOpenChange: (open: boolean) => void; commands: WorkbenchCommand[] }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return commands.filter((command) => !normalized || `${command.label} ${command.description}`.toLowerCase().includes(normalized));
  }, [commands, query]);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogSurface className="command-palette">
        <DialogTitle className="command-palette-title"><Command size={16} />命令面板<span>当前权限 / 事实投影</span></DialogTitle>
        <label className="command-search"><Search size={16} aria-hidden="true" /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工作区或动作…" aria-label="搜索命令" /></label>
        <div className="command-list" role="listbox" aria-label="命令列表">
          {filtered.map((command) => (
            <button key={command.id} type="button" className={`command-row ${command.enabled ? "" : "command-row-disabled"}`} disabled={!command.enabled} onClick={() => { void command.execute(); onOpenChange(false); }}>
              <span className="command-row-main"><strong>{command.label}</strong><small>{command.description}</small></span>
              <span className={`command-risk ${command.riskClass}`}>{command.enabled ? uiStatus(command.riskClass) : command.disabledReason}</span>
              {command.shortcut && <kbd>{command.shortcut}</kbd>}
            </button>
          ))}
          {!filtered.length && <div className="command-empty">没有匹配的命令</div>}
        </div>
        <div className="command-footer">Enter 执行 · Esc 关闭 · 危险动作不会绑定无确认快捷键</div>
      </DialogSurface>
    </Dialog>
  );
}
