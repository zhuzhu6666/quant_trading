import { useState, useEffect } from "react";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { KpiCard } from "@/components/dashboard/KpiCard";
import { authFetch } from "@/lib/auth";

const TABS = ["总览", "VaR", "压力", "集中度"];

export default function RiskPanel() {
  const [tab, setTab] = useState("总览");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Detail computation states
  const [varDetail, setVarDetail] = useState<any>(null);
  const [varDetailLoading, setVarDetailLoading] = useState(false);
  const [stressDetail, setStressDetail] = useState<any>(null);
  const [stressDetailLoading, setStressDetailLoading] = useState(false);
  const [concDetail, setConcDetail] = useState<any>(null);
  const [concDetailLoading, setConcDetailLoading] = useState(false);

  const runVarDetail = async () => {
    setVarDetailLoading(true);
    try {
      const r = await authFetch("/api/risk/var", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ equity_series: [] }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setVarDetail(await r.json());
    } catch (e: any) {
      setVarDetail({ error: e?.message ?? String(e) });
    } finally {
      setVarDetailLoading(false);
    }
  };

  const runStressDetail = async () => {
    setStressDetailLoading(true);
    try {
      const r = await authFetch("/api/risk/stress/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setStressDetail(await r.json());
    } catch (e: any) {
      setStressDetail({ error: e?.message ?? String(e) });
    } finally {
      setStressDetailLoading(false);
    }
  };

  const runConcDetail = async () => {
    setConcDetailLoading(true);
    try {
      const r = await authFetch("/api/risk/concentration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weights: [] }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setConcDetail(await r.json());
    } catch (e: any) {
      setConcDetail({ error: e?.message ?? String(e) });
    } finally {
      setConcDetailLoading(false);
    }
  };

  const fetchSummary = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await authFetch("/api/risk/summary");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSummary();
    const t = setInterval(fetchSummary, 30000);
    return () => clearInterval(t);
  }, []);

  const varData = data?.var;
  const kellyData = data?.kelly;
  const stressData = data?.stress;
  const concData = data?.concentration;

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

      {loading && (
        <div className="text-sm text-text-secondary text-center py-8">加载中...</div>
      )}
      {error && (
        <div className="text-sm text-danger bg-danger-light rounded-lg px-3 py-2">
          {error}
        </div>
      )}

      {/* 总览 */}
      {tab === "总览" && data && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <KpiCard
              label="VaR 95%"
              value={varData?.var_pct ? `${varData.var_pct}%` : "--"}
              subvalue={varData?.cvar ? `CVaR ${varData.cvar.toLocaleString()} USD` : "无数据"}
              trend={varData?.var_pct > 5 ? "down" : "neutral"}
            />
            <KpiCard
              label="Kelly"
              value={kellyData?.kelly_fraction ? `${(kellyData.kelly_fraction * 100).toFixed(1)}%` : "--"}
              subvalue={kellyData?.status === "no data" ? "无数据" : "半 Kelly 下注"}
              trend="neutral"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <KpiCard
              label="压力测试"
              value={stressData?.status === "ok" ? "通过" : "等待"}
              subvalue={stressData?.max_drawdown_pct ? `最大回撤 ${stressData.max_drawdown_pct}%` : "无数据"}
              trend={stressData?.status === "ok" ? "up" : "neutral"}
            />
            <KpiCard
              label="集中度"
              value={concData?.is_safe ? "安全" : "告警"}
              subvalue={concData?.max_single_weight ? `最大权重 ${(concData.max_single_weight * 100).toFixed(1)}%` : "无数据"}
              trend={concData?.is_safe ? "up" : "down"}
            />
          </div>
        </div>
      )}

      {/* VaR */}
      {tab === "VaR" && (
        <Card title="VaR / CVaR 计算" padding="md">
          <div className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">置信度</span>
              <span className="font-medium">{varData?.confidence ? `${(varData.confidence * 100).toFixed(0)}%` : "95%"}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">VaR</span>
              <span className="font-medium">{varData?.var !== undefined ? `$${varData.var.toLocaleString()}` : "--"}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">CVaR</span>
              <span className="font-medium">{varData?.cvar !== undefined ? `$${varData.cvar.toLocaleString()}` : "--"}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">当前权益</span>
              <span className="font-medium">{varData?.current_equity ? `$${varData.current_equity.toLocaleString()}` : "--"}</span>
            </div>
            <div className="flex items-center justify-between pt-2 border-t border-apple-divider">
              <span className="text-2xs text-text-secondary">POST equity_series 计算</span>
              <Button size="sm" variant="secondary" loading={varDetailLoading} onClick={runVarDetail}>
                计算详情
              </Button>
            </div>
            {varDetail && (
              <div className="text-xs bg-apple-bg rounded-lg p-3 space-y-1 font-mono">
                {varDetail.error ? (
                  <div className="text-danger">{varDetail.error}</div>
                ) : (
                  Object.entries(varDetail).map(([k, v]) => (
                    <div key={k} className="flex items-center justify-between">
                      <span className="text-text-secondary">{k}</span>
                      <span className="text-text-primary">{typeof v === "number" ? v.toLocaleString() : String(v)}</span>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </Card>
      )}

      {/* 压力 */}
      {tab === "压力" && (
        <div className="space-y-4">
          <Card title="压力测试场景" padding="md">
            {stressData?.scenarios && stressData.scenarios.length > 0 ? (
              <div className="space-y-3">
                {stressData.scenarios.map((s: any) => (
                  <div key={s.name} className="flex items-center justify-between py-2 border-b border-apple-divider last:border-0">
                    <div>
                      <div className="text-sm font-medium text-text-primary">{s.name}</div>
                      <div className="text-2xs text-text-secondary">
                        {s.shock_pct ? `冲击 ${(s.shock_pct * 100).toFixed(0)}%` : `波动率 x${s.vol_multiplier}`}
                      </div>
                    </div>
                    <Badge variant={s.survives ? "success" : "danger"}>
                      {s.survives ? "通过" : "失败"}
                    </Badge>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-sm text-text-secondary text-center py-8">
                暂无压力测试场景数据
              </div>
            )}
          </Card>
          <div className="flex justify-end">
            <Button size="sm" variant="secondary" loading={stressDetailLoading} onClick={runStressDetail}>
              计算详情
            </Button>
          </div>
          {stressDetail && (
            <div className="text-xs bg-apple-bg rounded-lg p-3 space-y-1 font-mono">
              {stressDetail.error ? (
                <div className="text-danger">{stressDetail.error}</div>
              ) : (
                Object.entries(stressDetail).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between">
                    <span className="text-text-secondary">{k}</span>
                    <span className="text-text-primary">{typeof v === "number" ? v.toLocaleString() : String(v)}</span>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      {/* 集中度 */}
      {tab === "集中度" && (
        <Card title="因子权重分布" padding="md">
          {concData?.max_single_name ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-secondary">最大单因子权重</span>
                <span className="font-medium">{concData.max_single_name} ({(concData.max_single_weight * 100).toFixed(1)}%)</span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-secondary">状态</span>
                <Badge variant={concData.is_safe ? "success" : "warning"}>
                  {concData.is_safe ? "安全" : "超阈"}
                </Badge>
              </div>
              {concData.alerts?.length > 0 && (
                <div className="text-sm text-danger bg-danger-light rounded-lg p-2">
                  {concData.alerts.map((a: string) => <div key={a}>{a}</div>)}
                </div>
              )}
            </div>
          ) : (
            <div className="text-sm text-text-secondary text-center py-8">
              暂无集中度数据
            </div>
          )}
          <div className="flex justify-end pt-3 border-t border-apple-divider mt-3">
            <Button size="sm" variant="secondary" loading={concDetailLoading} onClick={runConcDetail}>
              计算详情
            </Button>
          </div>
          {concDetail && (
            <div className="text-xs bg-apple-bg rounded-lg p-3 mt-3 space-y-1 font-mono">
              {concDetail.error ? (
                <div className="text-danger">{concDetail.error}</div>
              ) : (
                Object.entries(concDetail).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between">
                    <span className="text-text-secondary">{k}</span>
                    <span className="text-text-primary">{typeof v === "number" ? v.toLocaleString() : String(v)}</span>
                  </div>
                ))
              )}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
