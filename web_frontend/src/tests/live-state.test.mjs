import assert from "node:assert/strict";
import { isAuthenticationClose, isCompleteLiveSnapshot, reconnectDelay, shouldAcceptSnapshot, snapshotTimestamp } from "../hooks/liveStateLogic.ts";

assert.equal(isCompleteLiveSnapshot({ _fact: { envelope: "fact.v1", contract: "live.state.v2" } }), true);
assert.equal(isCompleteLiveSnapshot({ _fact: { envelope: "fact.v1", contract: "other" } }), false);
assert.equal(isCompleteLiveSnapshot({}), false);
assert.equal(shouldAcceptSnapshot(0, 100), true);
assert.equal(shouldAcceptSnapshot(100, 99), false);
assert.equal(shouldAcceptSnapshot(100, 100), true);
assert.equal(snapshotTimestamp({ generated_at: 100 }), 100);
assert.equal(snapshotTimestamp({ generated_at: "1970-01-01T00:01:40.000Z" }), 100);
assert.equal(reconnectDelay(1), 1500);
assert.equal(reconnectDelay(6, 250), 30000);
assert.equal(reconnectDelay(99, 999), 30000);
assert.equal(isAuthenticationClose(4001), true);
assert.equal(isAuthenticationClose(1006), false);
console.log("live state behavior: ok");
