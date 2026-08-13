import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = process.cwd();
const required = [
  "src/main.tsx", "src/App.tsx", "src/main.css", "src/api/client.ts", "src/api/fact.ts", "src/api/workbench.ts",
  "src/auth/tokenStore.ts", "src/cache/researchCache.ts", "src/cache/researchFallback.ts", "src/hooks/useLiveState.ts", "src/hooks/liveStateLogic.ts", "src/hooks/useNetworkStatus.ts", "src/i18n/zh-CN.ts", "src/shell/WorkbenchShell.tsx",
  "src/shell/SafetyRail.tsx", "src/shell/CommandPalette.tsx", "src/pages/TradeOpsPage.tsx", "src/pages/RiskDeskPage.tsx",
  "src/pages/ResearchPage.tsx", "src/pages/GovernancePage.tsx", "src/pages/OpsPage.tsx", "src/pages/LoginPage.tsx",
  "src-tauri/Cargo.toml", "src-tauri/src/lib.rs", "src-tauri/src/commands.rs", "src-tauri/src/secure_store.rs",
  "src/desktop/bridge.ts", "src/desktop/updater.ts", "src-tauri/tauri.conf.json", "src-tauri/tauri.release.conf.json", "src-tauri/capabilities/default.json",
];
for (const relative of required) assert.ok(fs.existsSync(path.join(root, relative)), `missing ${relative}`);

const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
assert.match(packageJson.dependencies.react, /^\^19/);
assert.ok(packageJson.dependencies["@radix-ui/react-dialog"]);
assert.ok(packageJson.devDependencies["@tauri-apps/cli"]);
assert.equal(packageJson.scripts.tauri, "tauri");

for (const old of ["OverviewPage.tsx", "TradingPage.tsx", "PnlPage.tsx", "RiskPage.tsx", "LearningPage.tsx", "ModelsPage.tsx", "EvidencePage.tsx", "V16BrainPage.tsx", "WorkspacePages.tsx"]) {
  assert.equal(fs.existsSync(path.join(root, "src/pages", old)), false, `legacy page remains: ${old}`);
}
console.log("workbench smoke test: ok");
