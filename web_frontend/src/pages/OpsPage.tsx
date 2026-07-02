import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Cpu, Database, RefreshCw, Server } from "lucide-react";
import {
  getBackendReadiness,
  getCtraderTokenStatus,
  getExternalDataStatus,
  getHealth,
  getOpsAlerts,
  getOpsRecovery,
  getSyncStatus,
  getSystemDbHealth,
} from "@/api/client";
import { MetricCard } from "@/components/Card";
import { Field, StatTile, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { formatTime } from "@/lib/format";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";

function dbFreshness(data: unknown): "ok" | "warn" | "bad" {
  const overall = pickString(data, ["overall", "status", "state"], "");
  if (overall === "healthy" || overall === "ok") return "ok";
  if (overall === "degraded" || overall === "warn") return "warn";
  return "bad";
}

function statusFromMaybeObject(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return pickString(value, ["status", "state", "overall"], "--");
}

function readableObjectLabel(value: unknown): string {
  if (value === undefined || value === null || value === "") return "--";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);

  const record = asRecord(value);
  const direct = pickString(record, ["name", "component", "key", "id", "message", "reason", "error"], "");
  if (direct) return translateDisplayValue(direct);

  const ok = pick(record, ["ok"]);
  const updatedAt = pick(record, ["updated_at", "updatedAt", "last_ts", "ts"]);
  if (updatedAt) {
    const status = typeof ok === "boolean" ? (ok ? "正常" : "异常") : "";
    return status ? `${formatTime(updatedAt)} · ${status}` : formatTime(updatedAt);
  }

  const entries = Object.entries(record)
    .filter(([, entryValue]) => entryValue !== undefined && entryValue !== null && typeof entryValue !== "object")
    .slice(0, 3)
    .map(([key, entryValue]) => `${key}: ${String(entryValue)}`);
  return entries.length ? entries.join(" · ") : "--";
}

function factorUpdateLabel(value: unknown): string {
  const record = asRecord(value);
  const last = pick(record, ["last_enrichment", "last_enrichment_ts", "updated_at"]);
  if (!last) return "--";
  if (typeof last === "string" || typeof last === "number") return formatTime(last);

  const lastRecord = asRecord(last);
  const updatedAt = pick(lastRecord, ["updated_at", "updatedAt", "ts"]);
  const ok = pickBoolean(lastRecord, ["ok"], true);
  const error = pickString(lastRecord, ["error", "message"], "");
  const timeLabel = updatedAt ? formatTime(updatedAt) : "--";
  if (error) return `${timeLabel} · ${error}`;
  return `${timeLabel} · ${ok ? "正常" : "异常"}`;
}

function summarizeIssues(raw: string): { short: string; full: string; count: number } {
  const full = raw.trim();
  if (!full) return { short: "--", full: "", count: 0 };
  const parts = full.split(";").map((item) => item.trim()).filter(Boolean);
  const count = parts.length || 1;
  const short = parts.slice(0, 2).join("; ");
  return {
    short: count > 2 ? `${short}; +${count - 2} 项` : short,
    full,
    count,
  };
}

export function OpsPage() {
  const healthQuery = useQuery({
    queryKey: ["ops-health"],
    queryFn: getHealth,
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
  const dbQuery = useQuery({
    queryKey: ["ops-db-health"],
    queryFn: getSystemDbHealth,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const readinessQuery = useQuery({
    queryKey: ["ops-backend-readiness"],
    queryFn: getBackendReadiness,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const alertsQuery = useQuery({
    queryKey: ["ops-alerts"],
    queryFn: getOpsAlerts,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const recoveryQuery = useQuery({
    queryKey: ["ops-recovery"],
    queryFn: getOpsRecovery,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const syncQuery = useQuery({
    queryKey: ["sync-status", "ops"],
    queryFn: getSyncStatus,
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const tokenQuery = useQuery({
    queryKey: ["ctrader-token-status", "ops"],
    queryFn: getCtraderTokenStatus,
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const externalQuery = useQuery({
    queryKey: ["external-data-status", "ops"],
    queryFn: getExternalDataStatus,
    refetchInterval: 60_000,
    staleTime: 20_000,
  });

  const health = asRecord(healthQuery.data);
  const backendHealth = pickString(health, ["status", "overall"], "--");
  const healthDbStatus = statusFromMaybeObject(pick(health, ["db"]));
  const healthTime = pick(health, ["server_time", "updatedAt", "checked_at"]);

  const backendReady = pickBoolean(readinessQuery.data, ["ready_for_frontend", "ready", "ok"], false);
  const backendSchema = pickString(readinessQuery.data, ["schema_version", "version"], "--");
  const readinessUpdated = pick(readinessQuery.data, ["generated_at", "updated_at", "ts"]);
  const live = pickRecord(readinessQuery.data, ["live"]) || {};
  const liveCtrader = pickRecord(live, ["ctrader"]) || {};
  const liveLoop = pickRecord(live, ["loop"]) || {};
  const liveReadiness = pickRecord(live, ["readiness"]) || {};
  const ctraderStatus = pickString(liveCtrader, ["status", "state"], statusFromMaybeObject(pick(health, ["ctrader"])));
  const loopRunning = pickBoolean(liveLoop, ["running"], false);
  const liveReadinessState = pickString(liveReadiness, ["state", "status"], "--");
  const readinessModel = pickRecord(readinessQuery.data, ["models"]) || {};
  const readinessService = pickRecord(readinessQuery.data, ["backend_service", "service_health"]) || {};
  const serviceStatus = pickString(readinessService, ["service_status", "serviceState", "status", "state"], "unknown");
  const modelEligible = pickBoolean(readinessModel, ["permission_ok", "eligible", "ok"], false);
  const blockers = pickArray(readinessQuery.data, ["blockers"]);
  const highLoad = pickRecord(readinessQuery.data, ["high_load"]) || {};
  const factorData = pickRecord(readinessQuery.data, ["factor_data"]) || {};
  const factorDataLast = factorUpdateLabel(factorData);
  const cpuHigh = pickBoolean(highLoad, ["high_cpu", "cpu_high"], false);
  const memoryHigh = pickBoolean(highLoad, ["high_memory", "memory_high"], false);
  const alerts = asRecord(alertsQuery.data);
  const alertStatus = pickString(alerts, ["status"], "--");
  const alertRules = pickNumber(alerts, ["rules_active"], pickArray(alerts, ["rules"]).length);
  const recovery = asRecord(recoveryQuery.data);
  const recoveryRunning = pickBoolean(recovery, ["running"], false);
  const loopHealthy = pickBoolean(recovery, ["loop_healthy"], false);
  const schedulerHealthy = pickBoolean(recovery, ["scheduler_healthy"], false);
  const recoveryFailures = pickNumber(recovery, ["failures"], 0);
  const sync = asRecord(syncQuery.data);
  const syncStatus = pickString(sync, ["last_status", "status"], "--");
  const syncLast = pick(sync, ["last_sync_utc", "last_sync", "updated_at"]);
  const syncTfCount = Object.keys(asRecord(pick(sync, ["per_tf"]))).length;
  const tokenStatus = asRecord(tokenQuery.data);
  const tokenOk = pickBoolean(tokenStatus, ["has_token"], false) && !pickBoolean(tokenStatus, ["expired"], false);
  const tokenHours = pickNumber(tokenStatus, ["remaining_hours"], 0);
  const externalSources = pickArray(externalQuery.data, ["sources"]);
  const externalStale = externalSources.filter((item) => pickBoolean(item, ["stale"], false) || pickString(item, ["status"], "") === "error").length;

  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "--");
  const dbChecked = pick(dbQuery.data, ["checked_at", "checkedAt", "updated_at"]);
  const dbFresh = dbFreshness(dbQuery.data);
  const dbSummary = pickRecord(dbQuery.data, ["summary"]) || {};
  const dbMissing = pickNumber(dbSummary, ["missing"], 0);
  const dbStale = pickNumber(dbSummary, ["stale"], 0);
  const dbFreshCount = pickNumber(dbSummary, ["fresh"], 0);
  const dbTotal = pickNumber(dbSummary, ["total"], 0);

  const dbList = useMemo(() => {
    const raw = pickArray(dbQuery.data, ["databases", "items", "database_list"]);
    return raw.map((entry) => {
      const item = pickRecord(entry, ["item"]) || asRecord(entry);
      return {
        name: pickString(item, ["name"], "--"),
        file: pickString(item, ["file", "path"], "--"),
        type: pickString(item, ["type", "role"], "--"),
        exists: pickBoolean(item, ["exists"], false),
        freshness: pickString(item, ["freshness", "status", "state"], "--"),
        totalRows: pickString(item, ["total_rows", "rows"], "0"),
        latestTs: pick(item, ["latest_ts", "latestTs", "updated_at"]),
        issues: summarizeIssues(pickArray(item, ["errors", "issues"]).map(readableObjectLabel).join("; ")),
      };
    });
  }, [dbQuery.data]);

  const dbProblemCount = dbList.filter((db) => !db.exists || db.issues.count > 0 || ["missing", "stale", "old", "error"].includes(db.freshness)).length;
  const hasError = healthQuery.isError || dbQuery.isError || readinessQuery.isError || alertsQuery.isError || recoveryQuery.isError || syncQuery.isError || tokenQuery.isError || externalQuery.isError;

  const refreshAll = () => {
    void healthQuery.refetch();
    void dbQuery.refetch();
    void readinessQuery.refetch();
    void alertsQuery.refetch();
    void recoveryQuery.refetch();
    void syncQuery.refetch();
    void tokenQuery.refetch();
    void externalQuery.refetch();
  };

  return (
    <section className="dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">运维</div>
          <h1>运维健康</h1>
          <p>后端服务、数据库、模型权限和高负载信号集中在这里。</p>
        </div>
        <div className="header-status">
          <StatusPill status={`接口 ${backendHealth}`} tone={toneFromStatus(backendHealth)} />
          <StatusPill status={`数据库 ${dbStatus}`} tone={dbFresh} />
          <StatusPill status={backendReady ? "前端就绪" : "前端受限"} tone={backendReady ? "ok" : "warn"} />
          <button className="header-refresh" type="button" onClick={refreshAll}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </div>

      <div className="stat-grid">
        <StatTile icon={Server} label="后端健康" value={translateDisplayValue(backendHealth)} detail={`cTrader ${translateDisplayValue(ctraderStatus)}`} tone={toneFromStatus(backendHealth)} />
        <StatTile icon={Database} label="数据库" value={translateDisplayValue(dbStatus)} detail={`新鲜 ${dbFreshCount} / 过期 ${dbStale} / 缺失 ${dbMissing}`} tone={dbFresh} />
        <StatTile icon={Activity} label="后端就绪" value={backendReady ? "已就绪" : "受限"} detail={`实时 ${translateDisplayValue(liveReadinessState)} · 阻断项 ${blockers.length}`} tone={backendReady ? "ok" : blockers.length ? "bad" : "warn"} />
        <StatTile icon={Cpu} label="高负载" value={cpuHigh || memoryHigh ? "有提示" : "正常"} detail={`CPU ${cpuHigh ? "高" : "正常"} · MEM ${memoryHigh ? "高" : "正常"}`} tone={cpuHigh || memoryHigh ? "warn" : "ok"} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="服务状态">
          <div className="field-list">
            <Field label="健康探针" value={backendHealth} tone={toneFromStatus(backendHealth)} />
            <Field label="cTrader" value={ctraderStatus} tone={toneFromStatus(ctraderStatus)} />
            <Field label="交易循环" value={loopRunning ? "运行中" : "未运行"} tone={loopRunning ? "ok" : "warn"} />
            <Field label="主库连接" value={healthDbStatus} tone={toneFromStatus(healthDbStatus)} />
            <Field label="最近健康时间" value={formatTime(healthTime)} />
            <Field label="请求异常" value={hasError ? "有" : "无"} tone={hasError ? "bad" : "ok"} />
          </div>
        </MetricCard>

        <MetricCard title="后端就绪">
          <div className="field-list">
            <Field label="合约版本" value={backendSchema} />
            <Field label="更新时间" value={formatTime(readinessUpdated)} />
            <Field label="服务状态" value={serviceStatus} tone={toneFromStatus(serviceStatus)} />
            <Field label="实时状态" value={liveReadinessState} tone={toneFromStatus(liveReadinessState)} />
            <Field label="模型权限" value={modelEligible ? "允许" : "受限"} tone={modelEligible ? "ok" : "warn"} />
            <Field label="高 CPU" value={cpuHigh ? "是" : "否"} tone={cpuHigh ? "warn" : "ok"} />
            <Field label="高内存" value={memoryHigh ? "是" : "否"} tone={memoryHigh ? "warn" : "ok"} />
            <Field label="因子最近更新" value={factorDataLast} />
          </div>
          <div className="compact-list">
            {blockers.length ? (
              blockers.slice(0, 10).map((blocker, index) => (
                <span className="data-badge data-badge-bad" key={`${readableObjectLabel(blocker)}-${index}`}>{readableObjectLabel(blocker)}</span>
              ))
            ) : (
              <span className="data-badge data-badge-ok">无阻断项</span>
            )}
          </div>
        </MetricCard>

        <MetricCard title="告警与恢复">
          <div className="field-list">
            <Field label="告警状态" value={alertStatus} tone={toneFromStatus(alertStatus)} />
            <Field label="活跃规则" value={alertRules} />
            <Field label="恢复守护" value={recoveryRunning ? "运行中" : "待命"} tone={recoveryRunning ? "warn" : "ok"} />
            <Field label="循环健康" value={loopHealthy ? "正常" : "异常"} tone={loopHealthy ? "ok" : "bad"} />
            <Field label="调度健康" value={schedulerHealthy ? "正常" : "异常"} tone={schedulerHealthy ? "ok" : "bad"} />
            <Field label="失败次数" value={recoveryFailures} tone={recoveryFailures ? "bad" : "ok"} />
          </div>
        </MetricCard>

        <MetricCard title="同步与外部数据">
          <div className="field-list">
            <Field label="同步状态" value={syncStatus} tone={toneFromStatus(syncStatus)} />
            <Field label="最近同步" value={formatTime(syncLast)} />
            <Field label="周期数" value={syncTfCount} />
            <Field label="cTrader Token" value={tokenOk ? "有效" : "异常"} tone={tokenOk ? "ok" : "bad"} />
            <Field label="Token 剩余" value={tokenHours ? `${Math.round(tokenHours)} 小时` : "--"} />
            <Field label="外部数据源" value={`${externalSources.length} 个`} />
            <Field label="外部过期/异常" value={externalStale} tone={externalStale ? "warn" : "ok"} />
          </div>
          <div className="compact-list">
            {externalSources.slice(0, 8).map((raw, index) => {
              const item = asRecord(raw);
              const sourceName = pickString(item, ["source", "table"], String(index + 1));
              const status = pickString(item, ["status"], "--");
              const stale = pickBoolean(item, ["stale"], false);
              return (
                <span className={`data-badge ${stale ? "data-badge-warn" : toneFromStatus(status) === "bad" ? "data-badge-bad" : "data-badge-ok"}`} key={`${sourceName}-${index}`}>
                  {sourceName} · {translateDisplayValue(status)}
                </span>
              );
            })}
          </div>
        </MetricCard>

        <MetricCard title="数据库健康" className="wide-panel">
          <div className="performance-row">
            <div>
              <span>数据库总数</span>
              <strong>{dbTotal || dbList.length}</strong>
            </div>
            <div>
              <span>新鲜</span>
              <strong>{dbFreshCount}</strong>
            </div>
            <div>
              <span>过期</span>
              <strong>{dbStale}</strong>
            </div>
            <div>
              <span>缺失</span>
              <strong>{dbMissing}</strong>
            </div>
          </div>
          <div className="field-list field-list-spaced">
            <Field label="数据库健康" value={dbStatus} tone={dbFresh} />
            <Field label="检查时间" value={formatTime(dbChecked)} />
            <Field label="异常数据库" value={dbProblemCount} tone={dbProblemCount ? "bad" : "ok"} />
          </div>
        </MetricCard>

        <MetricCard title="数据库清单" className="wide-panel">
          <div className="table-wrap ops-db-wrap">
            <table className="mobile-card-table ops-db-table">
              <colgroup>
                <col className="ops-db-name" />
                <col className="ops-db-file" />
                <col className="ops-db-type" />
                <col className="ops-db-status" />
                <col className="ops-db-rows" />
                <col className="ops-db-time" />
                <col className="ops-db-issues" />
              </colgroup>
              <thead>
                <tr>
                  <th>数据库</th>
                  <th>文件</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>行数</th>
                  <th>更新时间</th>
                  <th>异常</th>
                </tr>
              </thead>
              <tbody>
                {dbList.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="empty-state-small">暂未返回数据库清单</td>
                  </tr>
                ) : null}
                {dbList.map((db) => (
                  <tr key={`${db.name}-${db.file}`}>
                    <td>{db.name}</td>
                    <td>{db.file}</td>
                    <td>{db.type}</td>
                    <td><StatusPill status={db.exists ? db.freshness : "missing"} tone={db.exists ? toneFromStatus(db.freshness) : "bad"} /></td>
                    <td>{db.totalRows}</td>
                    <td>{formatTime(db.latestTs)}</td>
                    <td>
                      {db.issues.full ? (
                        <span className="issue-summary" title={db.issues.full}>{db.issues.short}</span>
                      ) : "--"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </MetricCard>

      </div>

      {hasError ? (
        <MetricCard title="接口异常">
          <ul className="error-list">
            {healthQuery.isError ? <li>health：{healthQuery.error instanceof Error ? healthQuery.error.message : "请求失败"}</li> : null}
            {dbQuery.isError ? <li>db-health：{dbQuery.error instanceof Error ? dbQuery.error.message : "请求失败"}</li> : null}
            {readinessQuery.isError ? <li>backend-readiness：{readinessQuery.error instanceof Error ? readinessQuery.error.message : "请求失败"}</li> : null}
            {alertsQuery.isError ? <li>alerts：{alertsQuery.error instanceof Error ? alertsQuery.error.message : "请求失败"}</li> : null}
            {recoveryQuery.isError ? <li>recovery：{recoveryQuery.error instanceof Error ? recoveryQuery.error.message : "请求失败"}</li> : null}
            {syncQuery.isError ? <li>sync：{syncQuery.error instanceof Error ? syncQuery.error.message : "请求失败"}</li> : null}
            {tokenQuery.isError ? <li>ctrader-token：{tokenQuery.error instanceof Error ? tokenQuery.error.message : "请求失败"}</li> : null}
            {externalQuery.isError ? <li>external-data：{externalQuery.error instanceof Error ? externalQuery.error.message : "请求失败"}</li> : null}
          </ul>
        </MetricCard>
      ) : null}
    </section>
  );
}
