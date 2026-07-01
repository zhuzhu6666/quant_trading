const DISPLAY_TRANSLATIONS: Record<string, string> = {
  API: "接口",
  DB: "数据库",
  Health: "健康探针",
  Readiness: "就绪",
  Live: "实时",
  Blocker: "阻断项",
  Fresh: "新鲜",
  Stale: "过期",
  Missing: "缺失",
  Decision: "裁决",
  Schema: "合约版本",
  Broker: "经纪商",
  "Circuit Breaker": "熔断器",

  ok: "正常",
  Healthy: "健康",
  healthy: "健康",
  connected: "已连接",
  ready: "就绪",
  running: "运行中",
  active: "活跃",
  online: "在线",
  degraded: "降级",
  unknown: "未知",
  idle: "空闲",
  warming: "预热中",
  warming_up: "预热中",
  limited: "受限",
  warn: "警告",
  error: "异常",
  failed: "失败",
  blocked: "已阻断",
  down: "离线",
  offline: "离线",
  missing: "缺失",
  stale: "过期",
  old: "过旧",
  fresh: "新鲜",
  recent: "较新",
  disabled: "未启用",
  enabled: "已启用",
  archived: "冷备",
  allowed: "允许",
  skipped: "已跳过",
  pending: "待处理",
  open_confirmed: "已开市",
  scheduled_open_fresh_quote: "交易时段内且报价新鲜",
  signal_below_threshold: "信号低于阈值",
  factor_pipeline_v4: "因子管道 v4",
  thesis: "交易假设",
  thesis_broken: "交易假设失效",

  proposed: "待审核",
  approved: "已批准",
  applied: "已应用",
  rejected: "已拒绝",
  rolled_back: "已回滚",
  rollback: "回滚",
  pending_review: "待审核",

  manual: "人工",
  demo_autonomous: "演示自治",
  advisory_only: "仅建议",
  shadow_only: "仅影子",
  live: "实盘",
  LIVE: "实盘",
  "DRY-RUN": "模拟",
  dry_run: "模拟",
  send: "下单",
  poll: "轮询",
  polling: "轮询",

  online_light: "在线轻量",
  offline_deep: "离线深度",

  HEALTHY: "健康",
  WATCH: "观察",
  DECAYING: "衰减",
  DEAD: "失效",
  UNKNOWN: "未知",

  LONG: "多",
  SHORT: "空",
  FLAT: "空仓",
  BUY: "买入",
  SELL: "卖出",

  good_win: "优质盈利",
  lucky_win: "侥幸盈利",
  good_loss: "可接受亏损",
  bad_loss: "劣质亏损",
  win: "盈利",
  loss: "亏损",

  full: "完整",
  partial: "部分",
  complete: "完整",
  incomplete: "不完整",
  valid: "有效",
  invalid: "无效",
  labeled: "已标注",
  unlabeled: "未标注",

  policy: "策略裁决",
  factor: "因子",
  parameter_template: "参数模板",
  position_supervisor_template: "持仓监督模板",
  supervisor_execution_trace: "监督执行轨迹",
  risk_reducing_action: "降低风险动作",

  open_trade: "开仓",
  tighten_position: "收紧保护",
  close_position: "关闭仓位",
  reduce_position: "减仓",
  switch_position_supervisor_template: "切换持仓监督模板",
  relax_thesis_break: "放宽交易假设失效阈值",
  fix_stop_legality: "修复止损合法性",
  tighten_profit_protection: "收紧盈利保护",
  increase_min_hold_window: "增加最小持仓观察窗口",
  boost_small: "小幅增权",
  downweight: "降权",
  watch: "继续观察",
  switch_parameter_template: "切换参数模板",
  risk_policy: "风控策略",
  var_gate: "VaR 闸门",
  supervisor_tighten: "监督收紧",
  broker_close: "经纪商平仓",

  register: "注册",
  promote: "提升",
  demote: "降级",
  review_only: "仅复核",
  manual_shadow_after_train: "训练后人工影子",
  offmarket_shadow_after_train: "闭市训练后影子",

  "position_supervisor:conservative.v1": "持仓监督:保守版.v1",
  supervisor_tighten_sltp: "监督止盈止损修复",
  di_spread: "DI 价差",
  ema_slope: "EMA 斜率",
  stoch_k: "随机指标 K",
};

const REASON_TRANSLATIONS: Record<string, string> = {
  "conservative supervisor template reduces small-loss full exits in offline replay":
    "保守版持仓监督模板在离线回放中减少了小亏损直接平仓",
  "historical samples show frequent tighten/reduce pressure before exits":
    "历史样本显示，退出前经常出现收紧保护或减仓压力",
  "multiple thesis-broken exits are small and early enough to require a minimum evidence window":
    "多次交易假设失效后的退出幅度较小且发生较早，建议增加最小证据观察窗口",
  "supervisor protection amendments had broker-side skip/failure evidence":
    "持仓保护修改存在经纪商侧跳过或失败证据，需要修复止损合法性",
};

export function translateDisplayValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "--";
  const text = String(value).trim();
  if (!text) return "--";

  const exact = DISPLAY_TRANSLATIONS[text] || DISPLAY_TRANSLATIONS[text.toLowerCase()];
  if (exact) return exact;

  return text.replace(/[A-Za-z][A-Za-z0-9_-]*/g, (token) => {
    return DISPLAY_TRANSLATIONS[token] || DISPLAY_TRANSLATIONS[token.toLowerCase()] || token;
  });
}

export function translateScopeLabel(scopeType: unknown, scopeKey: unknown): string {
  const type = translateDisplayValue(scopeType);
  const key = translateDisplayValue(scopeKey);
  return `${type} / ${key}`;
}

export function translateReasonText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "--";
  const text = String(value).trim();
  if (!text) return "--";

  const exact = REASON_TRANSLATIONS[text] || REASON_TRANSLATIONS[text.toLowerCase()];
  if (exact) return exact;

  const positiveFactor = text.match(/^factor\s+(.+?)\s+shows stable positive outcomes \((\d+) samples?\)$/i);
  if (positiveFactor) {
    return `因子 ${translateDisplayValue(positiveFactor[1])} 显示稳定正向结果（${positiveFactor[2]} 个样本）`;
  }

  const negativeFactor = text.match(/^factor\s+(.+?)\s+shows repeated negative outcomes \((\d+) samples?\)$/i);
  if (negativeFactor) {
    return `因子 ${translateDisplayValue(negativeFactor[1])} 多次出现负向结果（${negativeFactor[2]} 个样本）`;
  }

  const accumulatingFactor = text.match(/^factor\s+(.+?)\s+still accumulating evidence$/i);
  if (accumulatingFactor) {
    return `因子 ${translateDisplayValue(accumulatingFactor[1])} 仍在积累证据`;
  }

  const varGate = text.match(/^var_gate:\s*VaR=(.+)$/i);
  if (varGate) {
    return `VaR 闸门：VaR=${varGate[1]}`;
  }

  return translateDisplayValue(text);
}

export function translateScope(scope: string): string {
  const labels: Record<string, string> = {
    today: "今日",
    "24h": "24小时",
    "7d": "7天",
    "30d": "30天",
    all: "全部",
  };
  return labels[scope] || scope;
}
