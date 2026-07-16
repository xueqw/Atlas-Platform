#!/usr/bin/env node
/**
 * Deterministic tests for the Planner runtime CORE helpers (change:
 * refactor-planner-page-state, tasks 4.1–4.4).
 *
 * The runtime layer (usePlannerRuntime) wires React state + real WebSockets on
 * top of these pure helpers. The DECISION logic — working-set computation,
 * the concurrency gate, and connection recycling — lives here in runtime-core
 * so it can be proven without a browser, matching the behavior contract in
 * specs/conversational-agent-builder/spec.md:
 *
 *   - event routing is by conversation_id (proven via reduceWsEvent isolation
 *     test elsewhere); here we prove the runtime-management invariants:
 *   - a session "working" iff it has an in-flight generation
 *   - the concurrency gate blocks a NEW working session at the cap, but a
 *     session already working never counts against itself
 *   - connection recycling closes the least-recently-active idle sockets,
 *     never the active session and never one mid-generation
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_runtime_core.test.mjs
 *
 * Exit code 0 on success, 1 on first failing assertion.
 */
import { blankRuntime } from "../src/lib/planner-session.ts";
import {
  isWorking,
  computeWorkingIds,
  workingIdsChanged,
  canStartNewWorking,
  selectRecyclableConnections,
  MAX_WORKING_SESSIONS,
  MAX_LIVE_CONNECTIONS,
} from "../src/lib/planner/runtime-core.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}
function eq(actual, expected, name) {
  check(actual === expected, `${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// Helper: a runtime with a fake OPEN socket and a given lastActivity.
const OPEN = 1; // WebSocket.OPEN
function rtWith(cid, { thinking = false, streamBuf = "", activities = [], lastActivity = 0, open = true } = {}) {
  const rt = blankRuntime(cid);
  rt.thinking = thinking;
  rt.streamBuf = streamBuf;
  rt.activities = activities;
  rt.lastActivity = lastActivity;
  rt.ws = open ? { readyState: OPEN, close() {} } : null;
  return rt;
}

// ---- isWorking ----
eq(isWorking(rtWith("a")), false, "isWorking: idle session is not working");
eq(isWorking(rtWith("a", { thinking: true })), true, "isWorking: thinking → working");
eq(isWorking(rtWith("a", { streamBuf: "partial" })), true, "isWorking: mid-stream → working");
eq(isWorking(rtWith("a", { activities: [{ id: "1", phase: "p", label: "l", status: "active" }] })), true, "isWorking: active step → working");
eq(isWorking(rtWith("a", { activities: [{ id: "1", phase: "p", label: "l", status: "done" }] })), false, "isWorking: only done steps → not working");

// ---- computeWorkingIds ----
{
  const map = new Map([
    ["a", rtWith("a", { thinking: true })],
    ["b", rtWith("b")],
    ["c", rtWith("c", { streamBuf: "x" })],
  ]);
  const ids = computeWorkingIds(map.values());
  eq(ids.length, 2, "computeWorkingIds: counts only working sessions");
  check(ids.includes("a") && ids.includes("c"), "computeWorkingIds: returns the right cids");
}

// ---- workingIdsChanged ----
eq(workingIdsChanged(["a"], ["a"]), false, "workingIdsChanged: same set → false");
eq(workingIdsChanged(["a", "b"], ["a"]), true, "workingIdsChanged: added → true");
eq(workingIdsChanged(["a"], ["a", "b"]), true, "workingIdsChanged: removed → true");
eq(workingIdsChanged(["b"], ["a"]), true, "workingIdsChanged: swapped → true");

// ---- canStartNewWorking (concurrency gate, design D3) ----
eq(MAX_WORKING_SESSIONS, 3, "gate: cap is 3");
eq(canStartNewWorking("d", []), true, "gate: empty working set allows new");
eq(canStartNewWorking("d", ["a", "b"]), true, "gate: 2 working allows a 3rd new session");
eq(canStartNewWorking("d", ["a", "b", "c"]), false, "gate: 3 working blocks a NEW session");
eq(canStartNewWorking("a", ["a", "b", "c"]), true, "gate: a session already working never counts against itself");

// ---- selectRecyclableConnections (connection cap, design D5) ----
eq(MAX_LIVE_CONNECTIONS, 8, "recycle: cap is 8");
{
  // Under cap → nothing recycled.
  const live = Array.from({ length: 5 }, (_, i) => rtWith("c" + i, { lastActivity: i }));
  eq(selectRecyclableConnections(live, "c0").length, 0, "recycle: under cap recycles nothing");
}
{
  // 9 live, none thinking, active = newest. Should recycle exactly 1: the oldest idle.
  const live = Array.from({ length: 9 }, (_, i) => rtWith("c" + i, { lastActivity: i }));
  const active = "c8";
  const picked = selectRecyclableConnections(live, active);
  eq(picked.length, 1, "recycle: 9 live over cap of 8 → recycle 1");
  eq(picked[0].conversationId, "c0", "recycle: picks the least-recently-active");
}
{
  // The active session is the oldest — it must NOT be recycled; pick next oldest.
  const live = Array.from({ length: 9 }, (_, i) => rtWith("c" + i, { lastActivity: i }));
  const active = "c0"; // oldest is active
  const picked = selectRecyclableConnections(live, active);
  eq(picked.length, 1, "recycle: still recycles 1 when oldest is active");
  eq(picked[0].conversationId, "c1", "recycle: never recycles the active session");
}
{
  // The oldest is mid-generation (thinking) — it must NOT be recycled.
  const live = Array.from({ length: 9 }, (_, i) => rtWith("c" + i, { lastActivity: i, thinking: i === 0 }));
  const active = "c8";
  const picked = selectRecyclableConnections(live, active);
  eq(picked.length, 1, "recycle: recycles 1 when oldest is thinking");
  eq(picked[0].conversationId, "c1", "recycle: never recycles a session mid-generation");
}
{
  // 10 live, oldest two are active + thinking → recycle the next two oldest idle.
  const live = Array.from({ length: 10 }, (_, i) => rtWith("c" + i, { lastActivity: i, thinking: i === 1 }));
  const active = "c0";
  const picked = selectRecyclableConnections(live, active);
  eq(picked.length, 2, "recycle: 10 live over cap → recycle 2");
  check(!picked.some((r) => r.conversationId === "c0" || r.conversationId === "c1"),
    "recycle: skips both active(c0) and thinking(c1), picks next-oldest idle");
  eq(picked[0].conversationId, "c2", "recycle: first recycled is c2");
  eq(picked[1].conversationId, "c3", "recycle: second recycled is c3");
}

if (failed) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll runtime-core assertions passed");
