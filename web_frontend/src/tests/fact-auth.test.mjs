import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const client = read("src/api/client.ts");
const auth = read("src/contexts/AuthContext.tsx");
const actionButton = read("src/components/ActionButton.tsx");
const liveHook = read("src/hooks/useLiveState.ts");
const trading = read("src/pages/TradingPage.tsx");
const overview = read("src/pages/OverviewPage.tsx");
const app = read("src/App.tsx");
const ops = read("src/pages/OpsPage.tsx");
const fact = read("src/api/fact.ts");
const v16 = read("src/pages/V16BrainPage.tsx");

assert.match(client, /unauthorizedInFlight/);
assert.match(client, /refreshInFlight/);
assert.match(client, /runUnauthorizedOnce/);
assert.match(client, /navigator\.locks\.request\(AUTH_REFRESH_LOCK, refresh\)/);
assert.match(client, /currentToken && currentToken !== requestToken/);
assert.match(client, /refreshAccessToken\(requestToken\)/);
assert.match(auth, /window\.addEventListener\("storage", onStorage\)/);
assert.doesNotMatch(auth, /setUnauthorizedHandler\(\(\)\s*=>\s*\(\)\s*=>/);
assert.match(auth, /queryClient\.cancelQueries/);
assert.match(auth, /queryClient\.clear/);
assert.match(client, /postJson<StepUpResponse>\("\/api\/auth\/step-up", \{ password \}\)/);
assert.match(client, /publishAccessToken\(token\)/);
assert.match(client, /getApiErrorCode/);
assert.match(actionButton, /stepUpOnDemand/);
assert.match(actionButton, /autoComplete="current-password"/);
assert.match(actionButton, /role="alert"/);

assert.match(liveHook, /getWsTicket/);
assert.match(liveHook, /\?ticket=/);
assert.doesNotMatch(liveHook, /\?token=/);
assert.match(liveHook, /shouldApplyLiveSnapshot/);
assert.match(liveHook, /incomingSequence >= currentSequence/);
assert.match(liveHook, /hasSnapshotRef\.current/);
assert.match(liveHook, /LiveStateContext\.Provider/);
assert.match(app, /<LiveStateProvider enabled=\{authenticated\}>/);
assert.match(overview, /connected \? "WS 实时连接" : "轮询\/重连中"/);
assert.doesNotMatch(overview, /WS 事实未知/);
assert.match(overview, /queryKey: \["health"\][\s\S]*?refetchInterval: 3_000/);
assert.match(trading, /const connectionTone = connected \? "ok"/);

assert.match(fact, /missing_fact_envelope/);
assert.match(fact, /fact_contract_mismatch/);
assert.match(fact, /fact_source_unavailable/);
assert.match(fact, /state:\s*"unknown"/);
assert.match(trading, /disabled=\{!startFactsKnown \|\| loopRunning \|\| startBusy\}/);
assert.match(trading, /label="启动"[\s\S]*?stepUpOnDemand[\s\S]*?onAction=\{runStart\}/);
assert.match(trading, /label="停止"[\s\S]*?disabled=\{stopBusy\}/);
assert.match(trading, /label="紧急平仓"[\s\S]*?disabled=\{closeBusy\}/);
const emergencyAction = trading.match(/<ActionButton icon=\{RotateCcw\}[^\n]+/)?.[0] || "";
assert.doesNotMatch(emergencyAction, /stepUpOnDemand/);
assert.match(v16, /label="一次解锁"[\s\S]*?stepUpOnDemand[\s\S]*?onAction=\{runLiveUnlock\}/);
assert.match(ops, /!recoveryRegisteredVisible \? "未注册或未知"/);
assert.match(ops, /readFact\(recoveryQuery\.data, "ops\.auto-recovery\.v2"\)/);
assert.match(overview, /readFact\(queries\.session\.data, "live\.session-risk\.v2"\)/);
assert.match(ops, /!recoveryRegisteredVisible \? "未知" : loopHealthy/);
assert.match(ops, /!recoveryRegisteredVisible \? "未知" : schedulerHealthy/);
assert.match(ops, /loopHealthy && recoveryKnown \? "ok"/);
assert.match(ops, /schedulerHealthy && recoveryKnown \? "ok"/);
assert.match(ops, /"规则已配置 · 投递未知"/);

console.log("web fact/auth contract: ok");
