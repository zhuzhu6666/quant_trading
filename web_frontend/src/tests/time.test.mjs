import assert from "node:assert/strict";
import { epochSeconds, formatAgeSeconds, formatClock, formatObservedTime, formatTimestamp } from "../api/time.ts";
import { factAgeSeconds, readFact } from "../api/fact.ts";

assert.equal(epochSeconds(1_700_000_000_000), 1_700_000_000);
assert.equal(epochSeconds("1970-01-01T08:00:00+08:00"), 0);
assert.equal(formatTimestamp(0), "时间未知");
assert.match(formatTimestamp(1_700_000_000), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
assert.match(formatClock(1_700_000_000), /^\d{2}:\d{2}:\d{2}$/);
assert.match(formatObservedTime(1_700_000_000), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · /);
assert.equal(formatAgeSeconds(0), "刚刚");
assert.equal(formatAgeSeconds(12), "12秒前");
assert.equal(formatAgeSeconds(65), "1分05秒前");
assert.equal(formatAgeSeconds(null), "年龄未知");

const declaredKnown = readFact({
  _fact: {
    envelope: "fact.v1",
    contract: "live.account.v2",
    state: "known",
    source: "ctrader",
    observed_at: 1,
    generated_at: 2,
    stale_after_sec: 15,
    reason_code: null,
    components: {},
  },
}, "live.account.v2");

// The server owns the fact state.  A browser render must not silently create
// a second stale/known authority from its local wall clock.
assert.equal(declaredKnown.state, "known");
assert.equal(factAgeSeconds(declaredKnown, 20), 19);
console.log("desktop time contract: ok");
