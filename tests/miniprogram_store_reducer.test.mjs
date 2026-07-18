import assert from "node:assert/strict";
import { createStore } from "../miniprogram_v2/utils/store.js";

const store = createStore({ count: 0, nested: { value: 1 } });
const observed = [];
const unsubscribe = store.subscribe((state) => observed.push(state));

store.setState({ count: 1 });
assert.deepEqual(store.getState(), { count: 1, nested: { value: 1 } });
assert.equal(observed.length, 1);

const detached = store.getState();
detached.nested.value = 99;
assert.equal(store.getState().nested.value, 1, "getState must return a detached snapshot");

unsubscribe();
store.setState({ count: 2 });
assert.equal(observed.length, 1, "unsubscribe must stop reducer notifications");

console.log("miniprogram store reducer: ok");
