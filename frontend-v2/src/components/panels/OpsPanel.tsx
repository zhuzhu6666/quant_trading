import { useState, useEffect } from "react";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { KpiCard } from "@/components/dashboard/KpiCard";
import { authFetch } from "@/lib/auth";

const TABS = ["告警", "恢复", "周报", "实验"];

export default function OpsPanel() {
  const [tab, setTab] = useState("告警");
  const [alerts, setAlerts] = useState<any>(null);
  const [recovery, setRecovery] = useState<any>(null);
  const [reports, setReports] = useState<any>(null);
  const [experiments, setExperiments] = useState<any>(null);
  const [recoveryHistory, setRecoveryHistory] = useState<any[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [loading, setLoading] = useState(false);

  const fetchAlerts = async () => {
    try {
      const r = await authFetch("/api/ops/alerts");
      if (r.ok) setAlerts(await r.json());
    } catch { /* ignore */ }
  };

  const fetchRecovery = async () => {
    try {
      const r = await authFetch("/api/ops/recovery");
      if (r.ok) setRecovery(await r.json());
    } catch { /* ignore */ }
  };

  const fetchReports = async () => {
    try {
      const r = await authFetch("/api/ops/reports/weekly");
      if (r.ok) setReports(await r.json());
    } catch { /* ignore */ }
  };

  const fetchRecoveryHistory = async () => {
    try {
      const r = await authFetch("/api/ops/recovery/history");
      if (r.ok) setRecoveryHistory(await r.json());
    } catch { /* ignore */ }
  };

  const generateWeeklyReport = async () => {
    try {
      setLoading(true);
      const r = await authFetch("/api/ops/reports/weekly/generate", { method: "POST" });
      if (r.ok) {
        await fetchReports();
      }
    } catch { /* ignore */ }
    finally { setLoading(false); }
  };

  const fetchExperiments = async () => {
    try {
      const r = await authFetch("/api/experiments/");
      if (r.ok) setExperiments(await r.json());
    } catch { /* ignore */ }
  };

  useEffect(() => {
    fetchAlerts();
    fetchRecovery();
    fetchReports();
    fetchExperiments();
    const t = setInterval(() => { fetchAlerts(); fetchRecovery(); }, 30000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="space-y-4">
      {/* Tab Bar */}
      <div className="flex items-center gap-1 bg-apple-bg rounded-xl p-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 text-xs font-medium rounded-lg transition-all duration-200 ${
              tab === t
                ? "bg-white text-text-primary shadow-apple-sm"
                : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* 告警 */}
      {tab === "告警" && (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <KpiCard label="告警状态" value={alerts?.status ?? "--"} subvalue={`${alerts?.rules_active ?? 0} 条规则运行中`} trend="up" />
            <KpiCard label="本周告警" value="0" subvalue="无活跃告警" trend="neutral" />
            <KpiCard label="最近处理" value="--" subvalue="待接入" trend="neutral" />
          </div>
          <Card title="告警规则配置" padding="md">
            <div className="space-y-3">
              {(alerts?.rules ?? []).map((rule: any) => (
                <div key={rule.name} className="flex items-center justify-between py-2 border-b border-apple-divider last:border-0">
                  <div className="flex items-center gap-3">
                    <span className={`w-2 h-2 rounded-full ${rule.active ? "bg-success" : "bg-text-tertiary"}`} />
                    <span className="text-sm text-text-primary">{rule.name}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-2xs text-text-secondary">阈值 {rule.threshold}</span>
                    <Badge variant={rule.active ? "success" : "default"}>
                      {rule.active ? "启用" : "禁用"}
                    </Badge>
                  </div>
                </div>
              ))}
              {(!alerts?.rules || alerts.rules.length === 0) && (
                <div className="text-sm text-text-secondary text-center py-4">暂无规则</div>
              )}
            </div>
          </Card>
        </div>
      )}

      {/* 恢复 */}
      {tab === "恢复" && (
        <>
          <Card title="AutoRecovery 状态" padding="md">
            <div className="flex items-center gap-4 mb-4">
              <div className="flex items-center gap-2">
                <span className={`w-2.5 h-2.5 rounded-full ${recovery?.loop_healthy ? "bg-success" : "bg-warning"} animate-pulse-soft`} />
                <span className="text-sm text-text-primary font-medium">
                  {recovery?.running ? "30s 心跳正常" : "未启动"}
                </span>
              </div>
            </div>
            <div className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-text-secondary">运行中</span>
                <span className="text-text-primary">{recovery?.running ? "是" : "否"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-text-secondary">Loop 健康</span>
                <span className="text-text-primary">{recovery?.loop_healthy ? "健康" : "异常"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-text-secondary">调度健康</span>
                <span className="text-text-primary">{recovery?.scheduler_healthy ? "健康" : "异常"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-text-secondary">失败次数</span>
                <span className="text-text-primary">{recovery?.failures ?? 0}</span>
              </div>
            </div>
          </Card>
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setShowHistory(!showHistory);
                if (!showHistory && recoveryHistory.length === 0) {
                  fetchRecoveryHistory();
                }
              }}
            >
              {showHistory ? "收起历史" : "查看历史"}
            </Button>
          </div>
          {showHistory && (
            <Card title="恢复历史" padding="md">
              {recoveryHistory.length > 0 ? (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-apple-divider text-text-secondary text-2xs uppercase">
                      <th className="text-left py-2 pr-2">时间</th>
                      <th className="text-left py-2 pr-2">策略</th>
                      <th className="text-left py-2 pr-2">操作</th>
                      <th className="text-left py-2">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recoveryHistory.map((item: any, idx: number) => (
                      <tr key={idx} className="border-b border-apple-divider last:border-0">
                        <td className="py-2 pr-2 text-text-primary whitespace-nowrap">
                          {item.time ? new Date(item.time * 1000).toLocaleString() : "--"}
                        </td>
                        <td className="py-2 pr-2 text-text-primary">{item.strategy ?? "--"}</td>
                        <td className="py-2 pr-2 text-text-primary">{item.action ?? "--"}</td>
                        <td className="py-2">
                          <Badge variant={item.status === "success" ? "success" : "danger"}>
                            {item.status ?? "--"}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="text-sm text-text-secondary text-center py-4">暂无恢复历史</div>
              )}
            </Card>
          )}
        </>
      )}

      {/* 周报 */}
      {tab === "周报" && (
        <Card title="周报生成" padding="md">
          <div className="flex items-center justify-between mb-4">
            <div className="text-sm text-text-secondary">
              已生成: <span className="text-text-primary font-medium">{reports?.count ?? 0} 份</span>
            </div>
            <Button variant="primary" size="sm" onClick={generateWeeklyReport} loading={loading}>生成周报</Button>
          </div>
          <div className="space-y-2">
            {(reports?.reports ?? []).map((rep: any) => (
              <div key={rep.name} className="flex items-center justify-between py-2 border-b border-apple-divider last:border-0">
                <span className="text-sm text-text-primary">{rep.name}</span>
                <span className="text-2xs text-text-secondary">{new Date(rep.modified_at * 1000).toLocaleString()}</span>
              </div>
            ))}
            {(!reports?.reports || reports.reports.length === 0) && (
              <div className="text-sm text-text-secondary text-center py-4">暂无周报</div>
            )}
          </div>
        </Card>
      )}

      {/* 实验 */}
      {tab === "实验" && (
        <Card title="本周实验" padding="md">
          <div className="flex items-center justify-between mb-4">
            <div className="text-sm text-text-secondary">
              总计: <span className="text-text-primary font-medium">{experiments?.count ?? 0} 条</span>
            </div>
          </div>
          <div className="space-y-2">
            {(experiments?.experiments ?? []).slice(0, 10).map((exp: any) => (
              <div key={exp.run_id} className="flex items-center justify-between py-2 border-b border-apple-divider last:border-0">
                <div>
                  <span className="text-sm text-text-primary">{exp.experiment_type}</span>
                  <div className="text-2xs text-text-secondary">{exp.run_id.slice(0, 8)}</div>
                </div>
                <Badge variant={exp.status === "completed" ? "success" : exp.status === "failed" ? "danger" : "warning"}>
                  {exp.status}
                </Badge>
              </div>
            ))}
            {(!experiments?.experiments || experiments.experiments.length === 0) && (
              <div className="text-sm text-text-secondary text-center py-4">暂无实验</div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
