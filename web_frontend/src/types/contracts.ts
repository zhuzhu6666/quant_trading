import type { FactEnvelope, FactState } from "@/api/fact";

export type WorkspaceId = "trade-ops" | "risk-desk" | "research" | "governance" | "ops" | "workflow";

export type FactViewState = FactState;

export type ActionRiskClass = "risk-increase" | "risk-reduction" | "control" | "read-only";

export type ActionIntent = {
  actionId: string;
  scope: string;
  riskClass: ActionRiskClass;
  targetId?: string;
  requestedAt: string;
  clientRequestId: string;
  serverEndpoint: string;
  confirmation: "none" | "confirm" | "step-up";
};

export type MutationStatus = "pending" | "committed" | "rejected" | "aborted" | "unknown";

export type MutationResult = {
  status: MutationStatus;
  mutationId: string | null;
  auditId: string | null;
  commitStatus: "committed" | "pending" | "not_committed" | "unknown";
  reasonCode: string | null;
  observedAt: string | number | null;
};

export type CacheContract = "market.bars.v1" | "ops.replay.v2" | "factor.catalog.v4" | "research.snapshot.v1";

export type CacheEntry<T> = {
  cache_key: string;
  contract: CacheContract;
  schema_version: number;
  payload: T;
  source: string;
  observed_at: string | number;
  generated_at: string | number;
  expires_at: string | number;
  content_hash: string;
};

export type AccountFact = {
  fact: FactEnvelope;
  broker: string | null;
  balance: number | null;
  equity: number | null;
  margin: number | null;
  freeMargin: number | null;
  currency: string | null;
};

export type Position = {
  id: string;
  symbol: string;
  direction: "long" | "short" | "unknown";
  volume: number | null;
  entryPrice: number | null;
  currentPrice: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  unrealizedPnl: number | null;
  observedAt: string | number | null;
};

export type PositionsFact = {
  fact: FactEnvelope;
  positions: Position[] | null;
  brokerReconcile: {
    identity: FactEnvelope;
    protection: FactEnvelope;
    price: FactEnvelope;
    pnl: FactEnvelope;
  };
};

export type LoopFact = {
  fact: FactEnvelope;
  running: boolean | null;
  acceptingNewRisk: boolean | null;
  broker: string | null;
  reasonCode: string | null;
};

export type SessionRiskFact = {
  fact: FactEnvelope;
  pnlToday: number | null;
  tradeCount: number | null;
  drawdownPct: number | null;
  consecutiveLosses: number | null;
};

export type SpotFact = {
  fact: FactEnvelope;
  bid: number | null;
  ask: number | null;
  mid: number | null;
};

export type LiveStateSnapshot = {
  fact: FactEnvelope;
  serverTime: string | null;
  broker: string | null;
  account: AccountFact;
  positions: PositionsFact;
  loop: LoopFact;
  session: SessionRiskFact;
  spot: SpotFact;
  safety: FactEnvelope;
  safetyBlockers: string[];
  actionGates: Readonly<Record<string, boolean>>;
};

export type ReadinessDimension = {
  name: string;
  ready: boolean | null;
  reasonCode: string | null;
  observedAt: string | number | null;
};

export type ReadinessView = {
  fact: FactEnvelope;
  dimensions: ReadinessDimension[];
  blockers: string[];
  readyForFrontend: boolean | null;
  readyForLiveExecution: boolean | null;
  readyForLiveAlpha: boolean | null;
  readyForAutonomousMutation: boolean | null;
  readyForRelease: boolean | null;
};

export type RiskMetric = {
  status: "known" | "unknown" | "stale" | "error";
  value: number | null;
  unit: string;
  reasonCode: string | null;
};

export type RiskSnapshot = {
  schemaVersion: string | null;
  status: FactState;
  sampleCount: number | null;
  var95: RiskMetric;
  cvar95: RiskMetric;
  var99: RiskMetric;
  cvar99: RiskMetric;
  stressLossPct: RiskMetric;
  concentrationPct: RiskMetric;
  kellyFraction: RiskMetric;
};

export type RiskPolicyVerdict = {
  id: string;
  action: string;
  decision: "allow" | "block" | "unknown";
  reasonCode: string | null;
  decisionAt: string | number | null;
};

export type RiskDeskData = {
  fact: FactEnvelope;
  policyFact: FactEnvelope;
  traceFact: FactEnvelope;
  snapshot: RiskSnapshot;
  verdicts: RiskPolicyVerdict[];
  traceRows: ExecutionTrace[];
};

export type Bar = {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  spread: number;
};

export type MarketBars = {
  fact: FactEnvelope;
  symbol: string;
  timeframe: string;
  bars: Bar[];
  total: number;
  rangeFrom: number | null;
  rangeTo: number | null;
};

export type RealizedPnlScope = "today" | "24h" | "7d" | "30d" | "all";

export type RealizedPnlPoint = {
  ts: number;
  pnl: number;
  cumulative: number;
  source: string | null;
};

export type RealizedPnlSeries = {
  fact: FactEnvelope;
  scope: RealizedPnlScope;
  currency: string | null;
  fromTs: number | null;
  toTs: number | null;
  summary: {
    realizedPnl: number | null;
    trades: number | null;
    wins: number | null;
    losses: number | null;
    winRate: number | null;
  };
  points: RealizedPnlPoint[];
};

export type ResearchSnapshot = {
  fact: FactEnvelope;
  contract: string;
  title: string;
  referenceId: string | null;
  observedAt: string | number | null;
  status: "known" | "stale" | "unknown" | "error";
  rows: ResearchRow[];
};

export type ResearchRow = {
  id: string;
  title: string;
  state: string;
  reasonCode: string | null;
  observedAt: string | number | null;
  detail?: string | null;
};

export type LearningFactList<T> = {
  fact: FactEnvelope;
  items: T[];
  count: number;
};

export type LearningSampleRecord = {
  id: string;
  sampleType: string;
  labelStatus: string;
  integrity: string;
  trainWeight: number | null;
  modelReady: boolean | null;
  governanceEligible: boolean | null;
  systemContaminated: boolean | null;
  evidenceBlockers: string[];
  observedAt: string | number | null;
  updatedAt: string | number | null;
  symbol: string | null;
  positionId: string | null;
};

export type LearningReviewRecord = {
  id: string;
  tradeId: string | null;
  outcomeLabel: string | null;
  pnl: number | null;
  status: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
};

export type LearningDatasetQuality = {
  total: number;
  modelReady: number;
  needsAttention: number;
  readyRatio: number | null;
  avgQualityScore: number | null;
  missing: Record<string, number>;
};

export type LearningDatasetBlocker = {
  code: string;
  required: number | null;
  actual: number | null;
};

export type LearningDatasetReadinessView = {
  fact: FactEnvelope;
  ready: boolean | null;
  level: string | null;
  thresholds: Record<string, number>;
  quality: {
    trade: LearningDatasetQuality;
    decision: LearningDatasetQuality;
  };
  schemaIssueCount: number;
  blockers: LearningDatasetBlocker[];
  warnings: string[];
};

export type LearningQualityHealthView = {
  fact: FactEnvelope;
  evidenceCounts: Record<string, number>;
  evidenceExamples: Array<{ sampleId: string; codes: string[] }>;
  entryContextStatus: string | null;
  openDecisions: number;
  coverageRatio: Record<string, number>;
  missingTotal: number;
  maturedOpenOutcome: number;
};

export type LearningModelRecord = {
  id: string;
  status: string;
  modelType: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
};

export type LearningSuggestionRecord = {
  id: string;
  status: string;
  action: string | null;
  factorId: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
};

export type LearningApplicationRecord = {
  id: string;
  status: string;
  action: string | null;
  scope: string | null;
  observedAt: string | number | null;
  deltaAvgReward: number | null;
  postWinRate: number | null;
  baselineWinRate: number | null;
};

export type LearningEffectQualityView = {
  ok: boolean | null;
  status: string | null;
  statusCounts: Record<string, number>;
  reasonCounts: Record<string, number>;
  activeCount: number;
  terminalCount: number;
  closureRatio: number | null;
  boundedNonterminalCount: number;
  retryCandidateCount: number;
};

export type LearningLoopData = {
  samples: LearningFactList<LearningSampleRecord>;
  reviews: LearningFactList<LearningReviewRecord>;
  quality: LearningQualityHealthView;
  dataset: LearningDatasetReadinessView;
  shadowQueue: LearningFactList<LearningModelRecord>;
  inferenceAudits: LearningFactList<LearningModelRecord>;
  suggestions: LearningFactList<LearningSuggestionRecord>;
  governanceCandidates: { fact: FactEnvelope; items: GovernanceRecord[] };
  governanceReviews: { fact: FactEnvelope; items: GovernanceRecord[] };
  governanceProposals: { fact: FactEnvelope; items: GovernanceRecord[] };
  applications: LearningFactList<LearningApplicationRecord>;
  effectQuality: LearningEffectQualityView | null;
  effectQualityRequestFailed: boolean;
};

export type DecisionTrace = {
  traceId: string;
  decisionId: string | null;
  positionId: string | null;
  source: string;
  lineage: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
  eventType?: string | null;
  symbol?: string | null;
  timeframe?: string | null;
  direction?: string | null;
  actionReason?: string | null;
  systemView?: {
    direction: string | null;
    directionLabel: string | null;
    score: number | null;
    actionReason: string | null;
    outcomeStatus: string | null;
    outcomeResult: string | null;
    outcomeLabel: string | null;
    pnl: number | null;
    closeReason: string | null;
    summary: string | null;
  } | null;
  entryTs?: number | null;
  exitTs?: number | null;
  exitDecisionId?: string | null;
  closeReason?: string | null;
  holdingSeconds?: number | null;
  outcomeStatus?: string | null;
  outcomeResult?: string | null;
  outcomeLabel?: string | null;
  pnl?: number | null;
  learningStatus?: string | null;
  actionScore?: number | null;
};

export type ExecutionTrace = {
  id: string;
  stage: string;
  outcome: string;
  action: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
  tradeId?: string | null;
  positionId?: string | null;
  symbol?: string | null;
  summary?: string | null;
};

export type GovernanceRecord = {
  id: string;
  kind: "candidate" | "review" | "proposal" | "mutation" | "release" | "audit";
  status: string;
  durableId: string | null;
  auditId: string | null;
  commitStatus: string | null;
  reasonCode: string | null;
  observedAt: string | number | null;
  source?: string | null;
  stage?: string | null;
  action?: string | null;
  target?: string | null;
  authorityState?: string | null;
};

export type OpsHealth = {
  fact: FactEnvelope;
  source: {
    status: string | null;
    db: string | null;
    ctrader: string | null;
    serverTime: string | null;
    uptimeSeconds: number | null;
  };
};

export type SystemLoadView = {
  ok: boolean | null;
  observedAt: number | null;
  cpu: {
    percent: number | null;
    load1: number | null;
    load5: number | null;
    load15: number | null;
    cores: number | null;
  };
  memory: {
    percent: number | null;
    totalBytes: number | null;
    availableBytes: number | null;
    usedBytes: number | null;
  };
  disk: {
    path: string | null;
    percent: number | null;
    totalBytes: number | null;
    freeBytes: number | null;
    usedBytes: number | null;
  };
};

export type OpsLogSource = "backend" | "live_loop" | "alerts" | "debug";

export type OpsLogTail = {
  source: OpsLogSource;
  file: string | null;
  lines: string[];
  total: number;
  sizeBytes: number | null;
  observedAt: number | null;
};

export type RecoveryView = {
  fact: FactEnvelope;
  source: {
    status: string | null;
    registered: boolean | null;
    running: boolean | null;
    loopHealthy: boolean | null;
    schedulerHealthy: boolean | null;
    failures: number | null;
    lastCheck: number | null;
    restartAttempts: number | null;
  };
};

export type AlertsView = {
  fact: FactEnvelope;
  source: {
    status: string | null;
    configStatus: string | null;
    rulesActive: number | null;
    deliveryStatus: string | null;
    deliveryRegistered: boolean | null;
  };
};

export type IncidentControlView = {
  fact: FactEnvelope;
  effectiveMode: string | null;
  configuredMode: string | null;
  localSafetyLatch: boolean | null;
};
