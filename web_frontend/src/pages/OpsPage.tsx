import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, AlertTriangle, Bug, Clock3, FileWarning, Gauge, HardDrive, Pause, Play, RefreshCw, Search, Terminal, Wifi } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { getAlerts, getHealth, getLogTail, getRecovery, getSystemLoad } from "@/api/workbench";
import { formatTimestamp } from "@/api/time";
import { readDesktopDiagnostics } from "@/desktop/bridge";
import { FactBadge, Panel, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { uiStatus } from "@/i18n/zh-CN";
import type { OpsLogSource } from "@/types/contracts";
import { WorkspaceTitle } from "@/workspaces/WorkspaceBits";

type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL" | "UNKNOWN";

type LogEntry = {
  raw: string;
  timestamp: string;
  level: LogLevel;
  message: string;
};

type OpsIssue = {
  severity: "error" | "warning" | "info";
  title: string;
  detail: string;
  source: string;
};

const LOG_SOURCES: ReadonlyArray<{ id: OpsLogSource; label: string; file: string }> = [
  { id: "backend", label: "后台", file: "backend.log" },
  { id: "live_loop", label: "实时循环", file: "live_loop.log" },
  { id: "alerts", label: "告警", file: "alerts.log" },
  { id: "debug", label: "Debug", file: "debug.log" },
];

const LOG_LEVELS: ReadonlyArray<"ALL" | LogLevel> = ["ALL", "ERROR", "WARNING", "INFO", "DEBUG"];

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");

function parseLogLine(raw: string): LogEntry {
  const structured = raw.match(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*\|\s*(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s*\|\s*(.*)$/i);
  const bracketed = raw.match(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)?\s*(?:\|\s*)?\[?(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?\s*[:| -]?\s*(.*)$/i);
  const timestamp = structured?.[1] ?? bracketed?.[1] ?? raw.match(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/)?.[1] ?? "";
  const explicitLevel = structured?.[2] ?? bracketed?.[2] ?? raw.match(/\b(CRITICAL|FATAL|ERROR|WARNING|WARN|DEBUG|INFO|TRACE)\b/i)?.[1];
  const normalized = raw.toLowerCase();
  const level: LogLevel = explicitLevel
    ? explicitLevel.toUpperCase() === "WARN" || explicitLevel.toUpperCase() === "WARNING" ? "WARNING" : explicitLevel.toUpperCase() === "FATAL" ? "CRITICAL" : explicitLevel.toUpperCase() as LogLevel
    : /critical|fatal/.test(normalized)
      ? "CRITICAL"
      : /error|failed|failure|exception|traceback|崩溃|异常/.test(normalized)
        ? "ERROR"
        : /warn|degraded|timeout|retry|stale|blocked|降级|超时|重试/.test(normalized)
          ? "WARNING"
          : /debug/.test(normalized)
            ? "DEBUG"
            : "INFO";
  const message = structured?.[3] ?? bracketed?.[3] ?? raw;
  return {
    raw,
    timestamp: timestamp ? timestamp.replace("T", " ").split(/[.,]/)[0] : "—",
    level,
    message,
  };
}

function isHealthyStatus(value: string | null | undefined): boolean {
  return value ? ["ok", "healthy", "online", "running", "connected", "ready"].includes(value.toLowerCase()) : false;
}

function severityIcon(severity: OpsIssue["severity"]) {
  if (severity === "error") return <FileWarning size={16} />;
  if (severity === "warning") return <AlertTriangle size={16} />;
  return <Activity size={16} />;
}

function issueClass(severity: OpsIssue["severity"]): string {
  return `ops-issue ops-issue-${severity}`;
}

function logLevelLabel(level: LogLevel): string {
  return level === "WARNING" ? "WARN" : level;
}

function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "未知" : `${value.toFixed(1)}%`;
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(value / 1024)} KB`;
}

function RuntimeFact({ label, fact, detail }: { label: string; fact?: FactEnvelope; detail: string }) {
  return <div className="ops-runtime-fact"><div><strong>{label}</strong>{fact ? <FactBadge compact fact={fact} /> : null}</div><span>{detail}</span></div>;
}

export function OpsPage() {
  const live = useLiveState();
  const [activeSource, setActiveSource] = useState<OpsLogSource>("backend");
  const [levelFilter, setLevelFilter] = useState<"ALL" | LogLevel>("ALL");
  const [search, setSearch] = useState("");
  const [lineCount, setLineCount] = useState(240);
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const logViewportRef = useRef<HTMLDivElement>(null);
  const keepAtBottomRef = useRef(true);

  const health = useQuery({ queryKey: ["ops", "health"], queryFn: getHealth, staleTime: 30_000, retry: false });
  const recovery = useQuery({ queryKey: ["ops", "recovery"], queryFn: getRecovery, staleTime: 30_000, retry: false });
  const alerts = useQuery({ queryKey: ["ops", "alerts"], queryFn: getAlerts, staleTime: 30_000, retry: false });
  const systemLoad = useQuery({ queryKey: ["ops", "system-load"], queryFn: getSystemLoad, staleTime: 10_000, retry: false });
  const desktop = useQuery({ queryKey: ["ops", "desktop"], queryFn: readDesktopDiagnostics, staleTime: Infinity, retry: false });
  const logs = useQuery({ queryKey: ["ops", "logs", activeSource, lineCount], queryFn: () => getLogTail(activeSource, lineCount), staleTime: 2_000, refetchInterval: paused ? false : 3_000, retry: false });

  const healthFact = queryFact(health.data?.fact, health.error, "system.health.v2", "health_not_loaded");
  const recoveryFact = queryFact(recovery.data?.fact, recovery.error, "ops.auto-recovery.v2", "recovery_not_loaded");
  const alertsFact = queryFact(alerts.data?.fact, alerts.error, "ops.alerts.v2", "alerts_not_loaded");
  const allEntries = useMemo(() => (logs.data?.lines ?? []).map(parseLogLine), [logs.data?.lines]);
  const visibleEntries = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return allEntries.filter((entry) => (levelFilter === "ALL" || entry.level === levelFilter) && (!needle || `${entry.message} ${entry.raw}`.toLowerCase().includes(needle)));
  }, [allEntries, levelFilter, search]);
  const errorCount = allEntries.filter((entry) => entry.level === "ERROR" || entry.level === "CRITICAL").length;
  const warningCount = allEntries.filter((entry) => entry.level === "WARNING").length;

  useEffect(() => {
    const element = logViewportRef.current;
    if (!element || !autoScroll || !keepAtBottomRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [activeSource, autoScroll, logs.data?.lines]);

  const issues = useMemo<OpsIssue[]>(() => {
    const next: OpsIssue[] = [];
    const add = (severity: OpsIssue["severity"], title: string, detail: string, source: string) => next.push({ severity, title, detail, source });

    if (logs.error) add("error", "日志读取失败", "当前源没有返回可用尾部，先检查认证和后端接口。", "日志诊断");
    if (errorCount > 0) add("error", `日志窗口发现 ${errorCount} 条错误`, `${LOG_SOURCES.find((item) => item.id === activeSource)?.label ?? activeSource} · 这是诊断提示，不替代服务端事实。`, "日志诊断");
    if (warningCount > 0) add("warning", `日志窗口发现 ${warningCount} 条警告`, "包含降级、超时或重试等文本，点击日志终端查看上下文。", "日志诊断");
    if (health.error) add("error", "后端健康接口不可读", "服务健康事实请求失败，页面不会把失败解释成正常。", "/api/health");
    else if (!health.isPending && healthFact.state === "stale") add("warning", "后端健康事实已过期", healthFact.reason_code ?? "等待下一次服务端刷新。", "/api/health");
    else if (!health.isPending && healthFact.state !== "known") add("warning", "后端健康事实待确认", healthFact.reason_code ?? "当前没有可确认的健康状态。", "/api/health");
    if (health.data?.source.status && !isHealthyStatus(health.data.source.status)) add("warning", `后端报告：${uiStatus(health.data.source.status)}`, "保留服务端原始状态，未在前端重新计算健康结论。", "/api/health");
    if (recovery.data?.source.registered === false) add("warning", "自动恢复未注册", "只展示服务端诊断，不在页面新增恢复控制。", "/api/ops/recovery");
    if (recovery.data?.source.running === false && recovery.data?.source.registered === true) add("warning", "自动恢复未运行", "恢复任务已注册，但当前没有运行。", "/api/ops/recovery");
    if (alerts.data?.source.deliveryRegistered === false) add("warning", "告警投递未注册", "规则状态与投递状态分开显示，避免把告警配置误认为已送达。", "/api/ops/alerts");
    if (live.snapshot?.safetyBlockers.length) live.snapshot.safetyBlockers.slice(0, 3).forEach((blocker) => add("warning", "实时安全阻塞", blocker, "WS / live.state.v2"));
    if ((systemLoad.data?.disk.percent ?? 0) >= 90) add("warning", "磁盘空间接近阈值", `当前 ${formatPercent(systemLoad.data?.disk.percent)}，请查看服务器日志和磁盘占用。`, "/api/system/load");
    if (!next.length) add("info", "当前窗口未发现异常", "日志仍会持续轮询；异常摘要只汇总已读取到的诊断和服务端事实。", "诊断汇总");
    return next.slice(0, 8);
  }, [activeSource, alerts.data?.source.deliveryRegistered, errorCount, health.data?.source.status, health.error, healthFact.reason_code, healthFact.state, health.isPending, live.snapshot?.safetyBlockers, logs.error, recovery.data?.source.registered, recovery.data?.source.running, systemLoad.data?.disk.percent, warningCount]);

  const refreshAll = () => {
    void Promise.all([logs.refetch(), health.refetch(), recovery.refetch(), alerts.refetch(), systemLoad.refetch(), live.refresh()]);
  };

  const selectSource = (source: OpsLogSource) => {
    keepAtBottomRef.current = true;
    setActiveSource(source);
  };

  const logWindowSummary = logs.error ? "读取失败" : logs.data ? `${visibleEntries.length} / ${allEntries.length}` : "—";
  const issueSummary = health.isPending || logs.isPending ? "—" : String(issues.length);
  const loadSummary = systemLoad.data ? `CPU ${formatPercent(systemLoad.data.cpu.percent)} · 内存 ${formatPercent(systemLoad.data.memory.percent)}` : "—";

  return <div className="workspace-page ops-page">
    <WorkspaceTitle kicker="05 / 运维" title="运维中心" description="实时日志、异常摘要、任务诊断与审计追踪。" fact={healthFact} />
    <div className="workspace-toolbar ops-toolbar">
      <span><Terminal size={14} />日志 / {LOG_SOURCES.find((item) => item.id === activeSource)?.file}</span>
      <span><Wifi size={14} />WS / {uiStatus(live.connection)}</span>
      <FactBadge compact fact={healthFact} label="后端" />
      <span><Clock3 size={14} />观测 / {formatTimestamp(logs.data?.observedAt, "未读取")}</span>
      <button type="button" onClick={refreshAll} disabled={logs.isFetching}><RefreshCw size={14} />重新读取</button>
    </div>
    <div className="reference-fact-strip ops-summary-strip">
      <div className="reference-fact-card"><span>服务健康</span><strong><FactBadge compact fact={healthFact} /></strong><small>{health.data?.source.status ?? (health.error ? "请求失败" : "待确认")}</small></div>
      <div className="reference-fact-card"><span>日志窗口</span><strong>{logWindowSummary}</strong><small>{LOG_SOURCES.find((item) => item.id === activeSource)?.label} · {paused ? "已暂停" : "每 3 秒"}</small></div>
      <div className="reference-fact-card"><span>异常摘要</span><strong>{issueSummary}</strong><small>只汇总已读取诊断</small></div>
      <div className="reference-fact-card"><span>系统负载</span><strong>{loadSummary}</strong><small>{systemLoad.data ? "服务器响应" : "待确认"}</small></div>
      <div className="reference-fact-card"><span>自动恢复</span><strong>{recovery.data?.source.running === true ? "运行中" : recovery.data?.source.registered === true ? "已注册" : "—"}</strong><small><FactBadge compact fact={recoveryFact} /></small></div>
    </div>
    <div className="workspace-grid ops-grid">
      <Panel title="实时日志" eyebrow="/api/logs/tail" className="ops-log-panel">
        <div className="ops-log-toolbar">
          <div className="ops-log-source-tabs" role="tablist" aria-label="日志来源">
            {LOG_SOURCES.map((source) => <button key={source.id} type="button" role="tab" aria-selected={activeSource === source.id} className={activeSource === source.id ? "is-active" : ""} onClick={() => selectSource(source.id)}>{source.label}</button>)}
          </div>
          <label className="ops-log-filter"><Search size={14} /><span>筛选</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="关键字" /></label>
        </div>
        <div className="ops-log-controls">
          <label>级别 <select value={levelFilter} onChange={(event) => setLevelFilter(event.target.value as "ALL" | LogLevel)}>{LOG_LEVELS.map((level) => <option key={level} value={level}>{level === "ALL" ? "全部" : logLevelLabel(level)}</option>)}</select></label>
          <label>行数 <select value={lineCount} onChange={(event) => setLineCount(Number(event.target.value))}><option value={120}>120 行</option><option value={240}>240 行</option><option value={500}>500 行</option></select></label>
          <label className="ops-log-toggle"><input type="checkbox" checked={autoScroll} onChange={(event) => { setAutoScroll(event.target.checked); keepAtBottomRef.current = event.target.checked; }} />自动跟随</label>
          <button type="button" onClick={() => setPaused((value) => !value)}>{paused ? <Play size={14} /> : <Pause size={14} />}{paused ? "继续轮询" : "暂停轮询"}</button>
          <span className="ops-log-count">显示 {visibleEntries.length} / {allEntries.length} 行{logs.isFetching ? " · 读取中" : ""}</span>
        </div>
        <div className="ops-log-viewport" ref={logViewportRef} onScroll={(event) => { const element = event.currentTarget; keepAtBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 36; }} aria-live="polite">
          {visibleEntries.length ? visibleEntries.map((entry, index) => <div className={`ops-log-line ops-log-level-${entry.level.toLowerCase()}`} key={`${entry.raw}-${index}`} title={entry.raw}><span className="ops-log-line-number">{index + 1}</span><time>{entry.timestamp}</time><span className="ops-log-level">{logLevelLabel(entry.level)}</span><code>{entry.message}</code></div>) : <div className="ops-log-empty"><Bug size={18} /><strong>{logs.error ? "日志接口读取失败" : "当前筛选没有匹配日志"}</strong><span>{logs.error ? "请重新读取或检查服务端认证。" : "清空关键字或切换日志来源。"}</span></div>}
        </div>
        <div className="ops-log-footer"><span>源文件 / {logs.data?.file ?? LOG_SOURCES.find((item) => item.id === activeSource)?.file}</span><span>文件大小 / {formatBytes(logs.data?.sizeBytes)}</span><span>{paused ? "轮询已暂停" : "每 3 秒刷新"}</span>{!keepAtBottomRef.current && autoScroll ? <span className="ops-log-follow-hint">已保留当前位置 · 回到底部后继续跟随</span> : null}</div>
      </Panel>

      <Panel title="当前异常" eyebrow="诊断汇总" className="ops-signal-panel">
        <div className="ops-signal-note"><AlertTriangle size={15} /><span>只汇总已读取到的服务端事实和日志诊断，不重新计算风险或就绪度。</span></div>
        <div className="ops-issue-list">{issues.map((issue, index) => <div className={issueClass(issue.severity)} key={`${issue.source}-${issue.title}-${index}`}><span className="ops-issue-icon">{severityIcon(issue.severity)}</span><div><strong>{issue.title}</strong><span>{issue.detail}</span><small>{issue.source}</small></div></div>)}</div>
      </Panel>

      <Panel title="运行诊断" eyebrow="只读事实投影" className="ops-runtime-panel">
        <div className="ops-runtime-facts">
          <RuntimeFact label="自动恢复" fact={recoveryFact} detail={recovery.data?.source.running === true ? "运行中" : recovery.data?.source.registered === true ? "已注册 · 未确认运行" : "状态未知"} />
          <RuntimeFact label="告警投递" fact={alertsFact} detail={alerts.data?.source.deliveryRegistered === true ? "已注册" : alerts.data?.source.deliveryRegistered === false ? "未注册" : "状态未知"} />
          <RuntimeFact label="桌面壳" detail={desktop.data ? `${desktop.data.webview2 ?? "WebView2 未知"} · ${desktop.data.platform}` : "浏览器回退 / Tauri 诊断不可用"} />
        </div>
        <div className="ops-runtime-metrics"><span><Gauge size={14} />CPU {formatPercent(systemLoad.data?.cpu.percent)}</span><span><Activity size={14} />内存 {formatPercent(systemLoad.data?.memory.percent)}</span><span><HardDrive size={14} />磁盘 {formatPercent(systemLoad.data?.disk.percent)}</span></div>
        <div className="ops-runtime-note"><span>Debug 可见 · 日志仅用于定位上下文</span><span>重启次数 / {recovery.data?.source.restartAttempts ?? "未知"}</span></div>
        <SourceLine fact={healthFact} />
      </Panel>
    </div>
  </div>;
}
