import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import {
  getCtraderTokenStatus,
  getExternalDataStatus,
  getHealth,
  getLogTail,
  getOpsAlerts,
  getOpsRecovery,
  getSyncStatus,
  getSystemDbHealth,
} from "@/api/domains/system";
import { MetricCard } from "@/components/Card";
import { CompactMetric as OpsMiniMetric, Field, toneFromStatus } from "@/components/DashboardBits";
import { QueryErrorList } from "@/components/QueryErrorList";
import { StatusPill } from "@/components/StatusPill";
import { formatTime } from "@/lib/format";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact } from "@/api/fact";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { queryKeys } from "@/api/queryKeys";

function dbFreshness(data: unknown): "ok" | "warn" | "bad" {
  const overall = pickString(data, ["overall", "status", "state"], "");
  if (overall === "healthy" || overall === "ok") return "ok";
  if (overall === "degraded" || overall === "warn") return "warn";
  return "bad";
}

function statusFromMaybeObject(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return pickString(value, ["status", "state", "overall"], "");
}

function readableObjectLabel(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
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
  return entries.length ? entries.join(" · ") : "";
}

function factorUpdateLabel(value: unknown): string {
  const record = asRecord(value);
  const last = pick(record, ["last_enrichment", "last_enrichment_ts", "updated_at"]);
  if (!last) return "";
  if (typeof last === "string" || typeof last === "number") return formatTime(last);

  const lastRecord = asRecord(last);
  const updatedAt = pick(lastRecord, ["updated_at", "updatedAt", "ts"]);
  const ok = pickBoolean(lastRecord, ["ok"], true);
  const error = pickString(lastRecord, ["error", "message"], "");
  const timeLabel = updatedAt ? formatTime(updatedAt) : "";
  if (error) return `${timeLabel} · ${error}`;
  return `${timeLabel} · ${ok ? "正常" : "异常"}`;
}

function summarizeIssues(raw: string): { short: string; full: string; count: number } {
  const full = raw.trim();
  if (!full) return { short: "", full: "", count: 0 };
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
    queryKey: queryKeys.health,
    queryFn: getHealth,
    staleTime: 5_000,
  });
  const dbQuery = useQuery({
    queryKey: queryKeys.dbHealth,
    queryFn: getSystemDbHealth,
    staleTime: 5_000,
  });
  const readinessQuery = useBackendReadinessQuery();
  const alertsQuery = useQuery({
    queryKey: ["ops-alerts"],
    queryFn: getOpsAlerts,
    staleTime: 10_000,
  });
  const recoveryQuery = useQuery({
    queryKey: ["ops-recovery"],
    queryFn: getOpsRecovery,
    staleTime: 10_000,
  });
  const syncQuery = useQuery({
    queryKey: ["sync-status", "ops"],
    queryFn: getSyncStatus,
    staleTime: 20_000,
  });
  const tokenQuery = useQuery({
    queryKey: ["ctrader-token-status", "ops"],
    queryFn: getCtraderTokenStatus,
    staleTime: 20_000,
  });
  const externalQuery = useQuery({
    queryKey: ["external-data-status", "ops"],
    queryFn: getExternalDataStatus,
    staleTime: 20_000,
  });
  const logsQuery = useQuery({
    queryKey: queryKeys.logs(60),
    queryFn: () => getLogTail(60),
    staleTime: 10_000,
  });

  const health = asRecord(healthQuery.data);
  const healthFact = readFact(healthQuery.data, "system.health.v2");
  const dbFact = readFact(dbQuery.data, "system.db-health.v2");
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const recoveryFact = readFact(recoveryQuery.data, "ops.auto-recovery.v2");
  const alertsFact = readFact(alertsQuery.data, "ops.alerts.v2");
  const syncFact = readFact(syncQuery.data, "ops.sync-status.v2");
  const tokenFact = readFact(tokenQuery.data, "ops.ctrader-token-status.v2");
  const externalFact = readFact(externalQuery.data, "ops.external-data-status.v2");
  const healthRequestFailed = healthQuery.isError || healthQuery.isRefetchError;
  const dbRequestFailed = dbQuery.isError || dbQuery.isRefetchError;
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const alertsRequestFailed = alertsQuery.isError || alertsQuery.isRefetchError;
  const recoveryRequestFailed = recoveryQuery.isError || recoveryQuery.isRefetchError;
  const syncRequestFailed = syncQuery.isError || syncQuery.isRefetchError;
  const tokenRequestFailed = tokenQuery.isError || tokenQuery.isRefetchError;
  const externalRequestFailed = externalQuery.isError || externalQuery.isRefetchError;
  const logsRequestFailed = logsQuery.isError || logsQuery.isRefetchError;
  const healthKnown = factIsKnown(healthFact, healthRequestFailed);
  const dbKnown = factIsKnown(dbFact, dbRequestFailed);
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const alertsKnown = factIsKnown(alertsFact, alertsRequestFailed);
  const recoveryKnown = factIsKnown(recoveryFact, recoveryRequestFailed);
  const syncKnown = factIsKnown(syncFact, syncRequestFailed);
  const tokenKnown = factIsKnown(tokenFact, tokenRequestFailed);
  const externalKnown = factIsKnown(externalFact, externalRequestFailed);
  const backendHealth = pickString(health, ["status", "overall"], "");
  const healthDbStatus = statusFromMaybeObject(pick(health, ["db"]));
  const healthTime = pick(health, ["server_time", "updatedAt", "checked_at"]);

  const backendReadyReported = pickBoolean(readinessQuery.data, ["ready_for_frontend", "ready", "ok"], false);
  const backendSchema = pickString(readinessQuery.data, ["schema_version", "version"], "");
  const readinessUpdated = pick(readinessQuery.data, ["generated_at", "updated_at", "ts"]);
  const live = pickRecord(readinessQuery.data, ["live"]) || {};
  const liveCtrader = pickRecord(live, ["ctrader"]) || {};
  const liveLoop = pickRecord(live, ["loop"]) || {};
  const liveReadiness = pickRecord(live, ["readiness"]) || {};
  const ctraderStatus = pickString(liveCtrader, ["status", "state"], statusFromMaybeObject(pick(health, ["ctrader"])));
  const loopRunning = pickBoolean(liveLoop, ["running"], false);
  const liveReadinessState = pickString(liveReadiness, ["state", "status"], "");
  const readinessModel = pickRecord(readinessQuery.data, ["models"]) || {};
  const readinessService = pickRecord(readinessQuery.data, ["backend_service", "service_health"]) || {};
  const serviceStatus = pickString(readinessService, ["service_status", "serviceState", "status", "state"], "");
  const modelEligible = pickBoolean(readinessModel, ["permission_ok", "eligible", "ok"], false);
  const blockers = pickArray(readinessQuery.data, ["blockers"]);
  const highLoad = pickRecord(readinessQuery.data, ["high_load"]) || {};
  const factorData = pickRecord(readinessQuery.data, ["factor_data"]) || {};
  const factorDataLast = factorUpdateLabel(factorData);
  const cpuHigh = pickBoolean(highLoad, ["high_cpu", "cpu_high"], false);
  const memoryHigh = pickBoolean(highLoad, ["high_memory", "memory_high"], false);
  const alerts = asRecord(alertsQuery.data);
  const alertStatus = pickString(alerts, ["status"], "");
  const alertRules = pickNumber(alerts, ["rules_active"], pickArray(alerts, ["rules"]).length);
  const recovery = asRecord(recoveryQuery.data);
  const recoveryRegisteredReported = pickBoolean(recovery, ["registered"], pickString(recovery, ["status", "state"], "") !== "not_registered");
  const recoveryDisplayable = factHasDisplayValue(recoveryFact, recoveryRequestFailed);
  const recoveryRegisteredVisible = recoveryDisplayable && recoveryRegisteredReported;
  const recoveryRunning = pickBoolean(recovery, ["running"], false);
  const loopHealthy = pickBoolean(recovery, ["loop_healthy"], false);
  const schedulerHealthy = pickBoolean(recovery, ["scheduler_healthy"], false);
  const recoveryFailures = pickNumber(recovery, ["failures"], 0);
  const sync = asRecord(syncQuery.data);
  const syncStatus = pickString(sync, ["last_status", "status"], "");
  const syncLast = pick(sync, ["last_sync_utc", "last_sync", "updated_at"]);
  const syncTfCount = Object.keys(asRecord(pick(sync, ["per_tf"]))).length;
  const tokenStatus = asRecord(tokenQuery.data);
  const tokenOk = pickBoolean(tokenStatus, ["has_token"], false) && !pickBoolean(tokenStatus, ["expired"], false);
  const tokenHours = pickNumber(tokenStatus, ["remaining_hours"], 0);
  const externalSources = pickArray(externalQuery.data, ["sources"]);
  const externalStale = externalSources.filter((item) => pickBoolean(item, ["stale"], false) || pickString(item, ["status"], "") === "error").length;
  const logLines = pickArray(logsQuery.data, ["lines"]).map(String).filter((line) => line.trim()).slice(-60).reverse();

  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "");
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
        name: pickString(item, ["name"], ""),
        file: pickString(item, ["file", "path"], ""),
        type: pickString(item, ["type", "role"], ""),
        exists: pickBoolean(item, ["exists"], false),
        freshness: pickString(item, ["freshness", "status", "state"], ""),
        totalRows: pickString(item, ["total_rows", "rows"], "0"),
        latestTs: pick(item, ["latest_ts", "latestTs", "updated_at"]),
        issues: summarizeIssues(pickArray(item, ["errors", "issues"]).map(readableObjectLabel).join("; ")),
      };
    });
  }, [dbQuery.data]);

  const dbProblemCount = dbList.filter((db) => !db.exists || db.issues.count > 0 || ["missing", "stale", "old", "error"].includes(db.freshness)).length;
  const hasError = healthRequestFailed || dbRequestFailed || readinessRequestFailed || alertsRequestFailed || recoveryRequestFailed || syncRequestFailed || tokenRequestFailed || externalRequestFailed || logsRequestFailed;
  const allOpsFactsKnown = healthKnown && dbKnown && readinessKnown && alertsKnown && recoveryKnown && syncKnown && tokenKnown && externalKnown;

  const refreshAll = () => {
    void healthQuery.refetch();
    void dbQuery.refetch();
    void readinessQuery.refetch();
    void alertsQuery.refetch();
    void recoveryQuery.refetch();
    void syncQuery.refetch();
    void tokenQuery.refetch();
    void externalQuery.refetch();
    void logsQuery.refetch();
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
          <StatusPill status={factHasDisplayValue(healthFact, healthRequestFailed) ? `接口 ${backendHealth}` : "接口状态未知"} tone={factBoundTone(healthFact, toneFromStatus(backendHealth), healthRequestFailed)} fact={healthFact} requestFailed={healthRequestFailed} />
          <StatusPill status={factHasDisplayValue(dbFact, dbRequestFailed) ? `数据库 ${dbStatus}` : "数据库状态未知"} tone={factBoundTone(dbFact, dbFresh, dbRequestFailed)} fact={dbFact} requestFailed={dbRequestFailed} />
          <StatusPill status={factHasDisplayValue(readinessFact, readinessRequestFailed) ? (backendReadyReported ? "前端就绪" : "前端受限") : "前端就绪状态未知"} tone={factBoundTone(readinessFact, backendReadyReported ? "ok" : "warn", readinessRequestFailed)} fact={readinessFact} requestFailed={readinessRequestFailed} />
          <button className="header-refresh" type="button" onClick={refreshAll}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </div>

      <div className="dashboard-grid ops-dashboard-grid">
        <MetricCard title="运维控制台" className="wide-panel ops-control-panel">
          <div className="ops-mini-grid">
            <OpsMiniMetric label="后端健康" value={factHasDisplayValue(healthFact, healthRequestFailed) ? translateDisplayValue(backendHealth) : "未知"} detail={`cTrader ${translateDisplayValue(ctraderStatus)}`} tone={toneFromStatus(backendHealth)} fact={healthFact} requestFailed={healthRequestFailed} />
            <OpsMiniMetric label="数据库" value={factHasDisplayValue(dbFact, dbRequestFailed) ? translateDisplayValue(dbStatus) : "未知"} detail={`新鲜 ${dbFreshCount} · 过期 ${dbStale} · 缺失 ${dbMissing}`} tone={dbFresh} fact={dbFact} requestFailed={dbRequestFailed} />
            <OpsMiniMetric label="前端就绪" value={factHasDisplayValue(readinessFact, readinessRequestFailed) ? (backendReadyReported ? "已就绪" : "受限") : "未知"} detail={`阻断项 ${blockers.length} · ${translateDisplayValue(liveReadinessState)}`} tone={backendReadyReported ? "ok" : blockers.length ? "bad" : "warn"} fact={readinessFact} requestFailed={readinessRequestFailed} />
            <OpsMiniMetric label="高负载" value={factHasDisplayValue(readinessFact, readinessRequestFailed) ? (cpuHigh || memoryHigh ? "有提示" : "正常") : "未知"} detail={`CPU ${cpuHigh ? "高" : "正常"} · MEM ${memoryHigh ? "高" : "正常"}`} tone={cpuHigh || memoryHigh ? "warn" : "ok"} fact={readinessFact} requestFailed={readinessRequestFailed} />
            <OpsMiniMetric label="告警恢复" value={factHasDisplayValue(alertsFact, alertsRequestFailed) ? (alertRules ? `${alertRules} 规则` : "无规则") : "未知"} detail={recoveryRegisteredVisible ? `失败 ${recoveryFailures} · 守护 ${recoveryRunning ? "运行" : "待命"}` : "AutoRecovery 未注册或状态未知"} tone={recoveryFailures ? "bad" : "warn"} fact={alertsFact} requestFailed={alertsRequestFailed} />
            <OpsMiniMetric
              label="外部数据"
              value={externalKnown ? `${externalSources.length} 源` : "未知"}
              detail={externalKnown && syncKnown ? `异常/过期 ${externalStale} · TF ${syncTfCount}` : "等待后端事实"}
              tone={externalStale ? "warn" : externalKnown && syncKnown ? "ok" : "warn"}
              fact={externalFact}
              requestFailed={externalRequestFailed}
            />
          </div>

          <div className="ops-control-grid">
            <section className="ops-control-section">
              <div className="learning-section-head">
                <h3>服务链路</h3>
                <StatusPill status={serviceStatus} tone={factBoundTone(readinessFact, toneFromStatus(serviceStatus), readinessRequestFailed)} fact={readinessFact} requestFailed={readinessRequestFailed} />
              </div>
              <div className="field-list ops-compact-fields">
                <Field label="健康探针" value={factHasDisplayValue(healthFact, healthRequestFailed) ? backendHealth : "未知"} tone={factBoundTone(healthFact, toneFromStatus(backendHealth), healthRequestFailed)} fact={healthFact} requestFailed={healthRequestFailed} />
                <Field label="cTrader" value={ctraderStatus} tone={factBoundTone(readinessFact, toneFromStatus(ctraderStatus), readinessRequestFailed)} />
                <Field label="交易循环" value={loopRunning ? "运行中" : "未运行"} tone={factBoundTone(readinessFact, loopRunning ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="主库连接" value={healthDbStatus} tone={factBoundTone(healthFact, toneFromStatus(healthDbStatus), healthRequestFailed)} />
                <Field label="最近健康" value={formatTime(healthTime)} />
                <Field label="请求异常" value={hasError ? "有" : allOpsFactsKnown ? "无" : "事实未确认"} tone={hasError ? "bad" : allOpsFactsKnown ? "ok" : "warn"} />
              </div>
            </section>

            <section className="ops-control-section">
              <div className="learning-section-head">
                <h3>就绪与权限</h3>
                <StatusPill status={factHasDisplayValue(readinessFact, readinessRequestFailed) ? (backendReadyReported ? "前端就绪" : "前端受限") : "前端就绪状态未知"} tone={factBoundTone(readinessFact, backendReadyReported ? "ok" : "warn", readinessRequestFailed)} fact={readinessFact} requestFailed={readinessRequestFailed} />
              </div>
              <div className="field-list ops-compact-fields">
                <Field label="合约版本" value={backendSchema} />
                <Field label="更新时间" value={formatTime(readinessUpdated)} />
                <Field label="实时状态" value={liveReadinessState} tone={factBoundTone(readinessFact, toneFromStatus(liveReadinessState), readinessRequestFailed)} />
                <Field label="模型权限" value={modelEligible ? "允许" : "受限"} tone={factBoundTone(readinessFact, modelEligible ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="因子更新" value={factorDataLast} />
              </div>
              <div className="compact-list ops-inline-badges">
                {blockers.length ? (
                  blockers.slice(0, 8).map((blocker, index) => (
                    <span className="data-badge data-badge-bad" key={`${readableObjectLabel(blocker)}-${index}`}>{readableObjectLabel(blocker)}</span>
                  ))
                ) : readinessKnown ? (
                  <span className="data-badge data-badge-ok">无阻断项</span>
                ) : <span className="data-badge data-badge-warn">阻断项状态未知</span>}
              </div>
            </section>

            <section className="ops-control-section">
              <div className="learning-section-head">
                <h3>告警与恢复</h3>
                <StatusPill
                  status={!factHasDisplayValue(alertsFact, alertsRequestFailed) ? "未知" : recoveryRegisteredVisible ? alertStatus : "规则已配置 · 投递未知"}
                  tone={alertsKnown && recoveryKnown && recoveryRegisteredReported ? toneFromStatus(alertStatus) : "warn"}
                  fact={alertsFact}
                  requestFailed={alertsRequestFailed}
                />
              </div>
              <div className="field-list ops-compact-fields">
                <Field label="活跃规则" value={alertRules} />
                <Field
                  label="恢复守护"
                  value={!recoveryRegisteredVisible ? "未注册或未知" : recoveryRunning ? "运行中" : "待命"}
                  tone={!recoveryRegisteredVisible || recoveryRunning ? "warn" : recoveryKnown ? "ok" : "warn"}
                />
                <Field
                  label="循环健康"
                  value={!recoveryRegisteredVisible ? "未知" : loopHealthy ? "正常" : "异常"}
                  tone={!recoveryRegisteredVisible ? "warn" : loopHealthy && recoveryKnown ? "ok" : loopHealthy ? "pending" : "bad"}
                />
                <Field
                  label="调度健康"
                  value={!recoveryRegisteredVisible ? "未知" : schedulerHealthy ? "正常" : "异常"}
                  tone={!recoveryRegisteredVisible ? "warn" : schedulerHealthy && recoveryKnown ? "ok" : schedulerHealthy ? "pending" : "bad"}
                />
                <Field
                  label="失败次数"
                  value={recoveryRegisteredVisible ? recoveryFailures : "未知"}
                  tone={!recoveryRegisteredVisible ? "warn" : recoveryFailures ? "bad" : recoveryKnown ? "ok" : "warn"}
                />
              </div>
            </section>

            <section className="ops-control-section">
              <div className="learning-section-head">
                <h3>同步与外部</h3>
                <StatusPill status={syncStatus} tone={factBoundTone(syncFact, toneFromStatus(syncStatus), syncRequestFailed)} fact={syncFact} requestFailed={syncRequestFailed} />
              </div>
              <div className="field-list ops-compact-fields">
                <Field label="最近同步" value={formatTime(syncLast)} />
                <Field label="周期数" value={syncTfCount} />
                <Field label="cTrader Token" value={tokenOk ? "有效" : "异常"} tone={factBoundTone(tokenFact, tokenOk ? "ok" : "bad", tokenRequestFailed)} />
                <Field label="Token 剩余" value={tokenHours ? `${Math.round(tokenHours)} 小时` : ""} />
                <Field label="外部异常" value={externalStale} tone={factBoundTone(externalFact, externalStale ? "warn" : "ok", externalRequestFailed)} />
              </div>
              <div className="compact-list ops-inline-badges">
                {externalSources.slice(0, 8).map((raw, index) => {
                  const item = asRecord(raw);
                  const sourceName = pickString(item, ["source", "table"], String(index + 1));
                  const status = pickString(item, ["status"], "");
                  const stale = pickBoolean(item, ["stale"], false);
                  return (
                    <span className={`data-badge ${stale ? "data-badge-warn" : toneFromStatus(status) === "bad" ? "data-badge-bad" : externalKnown ? "data-badge-ok" : "data-badge-warn"}`} key={`${sourceName}-${index}`}>
                      {sourceName} · {translateDisplayValue(status)}
                    </span>
                  );
                })}
              </div>
            </section>
          </div>
        </MetricCard>

        <MetricCard title="数据库资产" className="wide-panel ops-db-panel">
          <div className="ops-db-summary">
            <OpsMiniMetric label="数据库总数" value={String(dbTotal || dbList.length)} detail={`检查 ${formatTime(dbChecked)}`} tone={dbFresh} fact={dbFact} requestFailed={dbRequestFailed} />
            <OpsMiniMetric label="新鲜" value={String(dbFreshCount)} detail={translateDisplayValue(dbStatus)} tone={dbKnown ? "ok" : "pending"} fact={dbFact} requestFailed={dbRequestFailed} />
            <OpsMiniMetric label="过期" value={String(dbStale)} detail="需要关注 freshness" tone={dbStale ? "warn" : dbKnown ? "ok" : "pending"} fact={dbFact} requestFailed={dbRequestFailed} />
            <OpsMiniMetric label="缺失" value={String(dbMissing)} detail="文件或注册缺失" tone={dbMissing ? "bad" : dbKnown ? "ok" : "pending"} fact={dbFact} requestFailed={dbRequestFailed} />
            <OpsMiniMetric label="异常数据库（前端汇总）" value={dbKnown ? String(dbProblemCount) : "未知"} detail="缺失/过期/错误" tone={dbProblemCount ? "bad" : dbKnown ? "ok" : "pending"} fact={dbFact} requestFailed={dbRequestFailed} />
          </div>

          <div className="ops-db-card-grid">
            {!dbList.length ? <div className="empty-state-small">暂未返回数据库清单</div> : null}
            {dbList.map((db) => (
              <article className="ops-db-card" key={`${db.name}-${db.file}`}>
                <div className="ops-db-card-head">
                  <div>
                    <strong>{db.name}</strong>
                    <span>{db.file}</span>
                  </div>
                  <StatusPill status={db.exists ? db.freshness : "missing"} tone={db.exists ? factBoundTone(dbFact, toneFromStatus(db.freshness), dbRequestFailed) : "bad"} fact={dbFact} requestFailed={dbRequestFailed} />
                </div>
                <div className="ops-db-card-meta">
                  <span>{translateDisplayValue(db.type)}</span>
                  <span>{db.totalRows} 行</span>
                  <span>{formatTime(db.latestTs)}</span>
                </div>
                {db.issues.full ? <p title={db.issues.full}>{db.issues.short}</p> : dbKnown ? <p className="ops-db-ok">无异常</p> : <p>无已确认异常 · 事实未确认</p>}
              </article>
            ))}
          </div>
        </MetricCard>

        <details className="detail-disclosure wide-panel">
          <summary>实时日志 · 最近 {logLines.length} 行</summary>
          <pre className="overview-log-scroll">{logLines.join("\n") || (logsQuery.isError ? "日志读取失败" : "暂无日志")}</pre>
        </details>
      </div>

      {hasError ? (
        <MetricCard title="接口异常">
          <QueryErrorList queries={[
            { label: "health", query: healthQuery },
            { label: "db-health", query: dbQuery },
            { label: "backend-readiness", query: readinessQuery },
            { label: "alerts", query: alertsQuery },
            { label: "recovery", query: recoveryQuery },
            { label: "sync", query: syncQuery },
            { label: "ctrader-token", query: tokenQuery },
            { label: "external-data", query: externalQuery },
            { label: "logs", query: logsQuery },
          ]} />
        </MetricCard>
      ) : null}
    </section>
  );
}
