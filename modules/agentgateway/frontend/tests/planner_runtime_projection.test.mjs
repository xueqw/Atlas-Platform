#!/usr/bin/env node
/**
 * Deterministic tests for the Planner runtime PROJECTION + ACTION-gate invariants
 * (change: refactor-planner-page-state, tasks 4.1, 4.2, 4.5).
 *
 * usePlannerRuntime keeps a Map<cid, SessionRuntime> and projects ONLY the active
 * runtime into the view; usePlannerActions gates a new turn through the runtime's
 * working set. Both layers wire React state + sockets we can't mount headless, so
 * here we prove the underlying invariants the wiring relies on — using the same
 * pure pieces the hooks call (reduceWsEvent + runtime-core), with a tiny model of
 * "what the view would project":
 *
 *   4.1 — an event routed into the OWNING (non-active) runtime mutates only that
 *         runtime; the active runtime (hence the projected view) is untouched.
 *   4.2 — switching the active cid changes which runtime the view projects from;
 *         no state bleeds across the switch.
 *   4.5 — the send gate (canStartNewWorking) admits/blocks exactly as the action
 *         layer would before dispatching a turn.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_runtime_projection.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";
import { computeWorkingIds, canStartNewWorking } from "../src/lib/planner/runtime-core.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}
function eq(actual, expected, name) {
  check(actual === expected, `${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// Minimal model of the hook's project-if-active rule: the view reads from the
// runtime whose cid === active, exactly like syncViewFromRuntime/projectIfActive.
function projectedView(map, activeCid) {
  const rt = map.get(activeCid);
  if (!rt) return null;
  return { messages: rt.messages, thinking: rt.thinking, streaming: rt.streamBuf, stage: rt.stage };
}

// ---- 4.1: event into a non-active runtime does not touch the active view ----
{
  const map = new Map([
    ["A", blankRuntime("A")],
    ["B", blankRuntime("B")],
  ]);
  const active = "A"; // focused session
  const before = projectedView(map, active);

  // A turn is in flight on B (the BACKGROUND session): the action layer sets
  // thinking=true, then a streamed token arrives for B. Both must fold into B
  // only — never into the active (A) view.
  map.get("B").thinking = true;
  reduceWsEvent(map.get("B"), { type: "thinking_content", content: "B is reasoning" });
  reduceWsEvent(map.get("B"), { type: "token", content: "hello from B" });

  const after = projectedView(map, active);
  eq(after.thinking, before.thinking, "4.1: active view's thinking unchanged by B's event");
  eq(after.streaming, "", "4.1: active view's stream stays empty (B's token didn't bleed in)");
  eq(map.get("B").streamBuf, "hello from B", "4.1: B's own runtime accumulated the token");
  check(map.get("B").thinkBuf === "B is reasoning", "4.1: B's own runtime captured its reasoning");
}

// ---- 4.2: switching active cid re-points the projection, no bleed ----
{
  const map = new Map([
    ["A", blankRuntime("A")],
    ["B", blankRuntime("B")],
  ]);
  // Give each a distinct in-flight state (thinking set by the action layer; a
  // token streamed in via the reducer, mirroring real turn dispatch).
  map.get("A").thinking = true;
  reduceWsEvent(map.get("B"), { type: "token", content: "B-stream" });

  const viewA = projectedView(map, "A");
  eq(viewA.thinking, true, "4.2: while active=A, view shows A's thinking");
  eq(viewA.streaming, "", "4.2: while active=A, B's stream is not visible");

  const viewB = projectedView(map, "B");
  eq(viewB.streaming, "B-stream", "4.2: after switching active→B, view shows B's stream");
  eq(viewB.thinking, false, "4.2: after switch, A's thinking is not visible on B");
}

// ---- 4.5: send gate admits/blocks like the action layer ----
{
  // Build a working set from a runtime map (as recomputeWorking does). A session
  // is "working" once a turn is in flight — thinking flag (set on dispatch) or a
  // non-empty stream buffer.
  const map = new Map([
    ["A", blankRuntime("A")],
    ["B", blankRuntime("B")],
    ["C", blankRuntime("C")],
    ["D", blankRuntime("D")],
  ]);
  map.get("A").thinking = true;
  map.get("B").thinking = true;
  map.get("C").thinking = true;
  const working = computeWorkingIds(map.values());
  eq(working.length, 3, "4.5: three sessions working");

  // D is a NEW session and the cap (3) is reached → blocked.
  eq(canStartNewWorking("D", working), false, "4.5: send blocked for a new session at the cap");
  // A is already working → its own next turn is never blocked by itself.
  eq(canStartNewWorking("A", working), true, "4.5: a working session can still send (counts itself)");
}

if (failed) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll runtime-projection assertions passed");
