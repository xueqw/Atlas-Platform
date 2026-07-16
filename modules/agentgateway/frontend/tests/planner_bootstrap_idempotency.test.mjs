#!/usr/bin/env node
/**
 * Deterministic tests for the planner page bootstrap orchestration (change:
 * fix-replan-bootstrap-race, tasks 3.1 / 3.2).
 *
 * The DeerFlow planner page mounts a one-shot effect that picks exactly ONE of
 * four routes (from_agent / ?session / local-cache / fresh-start) to open a
 * session. Under React StrictMode the effect mounts twice and the first mount
 * may be cancelled mid-`await`. The bug: the latch was set on *entry*, so an
 * aborted first mount consumed it and the second mount fell through to the
 * fresh-start backstop, spawning a stray blank `mode=create` session.
 *
 * runPlannerBootstrap is the pure orchestrator the real effect calls; these
 * tests drive it directly with injected actions + a scripted cancel timing —
 * no browser, no React, no flake — to prove the idempotency invariants from
 * specs/deerflow-planner-experience/spec.md ("规划页 bootstrap 引导必须幂等").
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_bootstrap_idempotency.test.mjs
 *
 * Exit code 0 on success, 1 on first failing assertion.
 */
import { runPlannerBootstrap } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}
function eq(actual, expected, name) {
  check(actual === expected, `${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// A spy harness modeling the injected actions + their call counts. `opened`
// records the conversation_id of whichever route actually focused a session.
function makeSpies(overrides = {}) {
  const calls = { createFromAgent: 0, openExisting: 0, restoreCache: 0, startNew: 0, fromAgentError: 0 };
  let opened = null;
  const spies = {
    calls,
    get opened() { return opened; },
    createSessionFromAgent: async (id) => {
      calls.createFromAgent++;
      if (overrides.createThrows) throw new Error("boom");
      // Backend is idempotent: same agent → same linked conversation_id.
      return { conversation_id: `conv-for-agent-${id}` };
    },
    openExistingSession: async (cid) => {
      calls.openExisting++;
      if (overrides.openFails) return false;
      opened = cid;
      return true;
    },
    restoreFromCache: () => {
      calls.restoreCache++;
      if (overrides.cacheHit) { opened = "conv-from-cache"; return true; }
      return false;
    },
    startNewSession: async () => {
      calls.startNew++;
      opened = "conv-fresh";
      return true;
    },
    onFromAgentError: () => { calls.fromAgentError++; },
  };
  return spies;
}

// --- 1) Single mount, from_agent: opens the linked session, no fresh-start ---
{
  const s = makeSpies();
  const restored = { current: false };
  const cancelled = { current: false };
  await runPlannerBootstrap({
    fromAgentId: 8, sessionParam: null, restored, cancelled,
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.opened, "conv-for-agent-8", "single-mount from_agent opens linked session");
  eq(restored.current, true, "single-mount from_agent latches restored");
  eq(s.calls.startNew, 0, "single-mount from_agent never calls startNewSession");
  eq(s.calls.restoreCache, 0, "single-mount from_agent never restores cache");
}

// --- 2) StrictMode double-mount, mount#1 cancelled AFTER createSessionFromAgent
//        but BEFORE open. This is the exact replan bug. mount#2 must re-run the
//        from_agent route and open it — and crucially NEVER call startNewSession.
{
  const s = makeSpies();
  const restored = { current: false }; // shared ref survives across remounts
  // mount #1: its own cancelled flag, flipped true by the "unmount" before open.
  const cancelled1 = { current: false };
  // Simulate: createSessionFromAgent resolves, then cleanup fires (cancel), then
  // the orchestrator checks cancelled and bails without opening or latching.
  const s1 = makeSpies();
  s1.createSessionFromAgent = async (id) => {
    s1.calls.createFromAgent++;
    cancelled1.current = true; // unmount happens during the await
    return { conversation_id: `conv-for-agent-${id}` };
  };
  await runPlannerBootstrap({
    fromAgentId: 8, sessionParam: null, restored, cancelled: cancelled1,
    createSessionFromAgent: s1.createSessionFromAgent,
    openExistingSession: s1.openExistingSession,
    restoreFromCache: s1.restoreFromCache,
    startNewSession: s1.startNewSession,
    onFromAgentError: s1.onFromAgentError,
  });
  eq(restored.current, false, "aborted mount#1 leaves latch UNSET");
  eq(s1.opened, null, "aborted mount#1 opens nothing");
  eq(s1.calls.openExisting, 0, "aborted mount#1 does not reach openExistingSession");
  eq(s1.calls.startNew, 0, "aborted mount#1 does not fall through to startNewSession");

  // mount #2: fresh cancelled flag (not cancelled), same shared `restored`.
  const cancelled2 = { current: false };
  const s2 = makeSpies();
  await runPlannerBootstrap({
    fromAgentId: 8, sessionParam: null, restored, cancelled: cancelled2,
    createSessionFromAgent: s2.createSessionFromAgent,
    openExistingSession: s2.openExistingSession,
    restoreFromCache: s2.restoreFromCache,
    startNewSession: s2.startNewSession,
    onFromAgentError: s2.onFromAgentError,
  });
  eq(s2.opened, "conv-for-agent-8", "mount#2 re-runs from_agent and opens the linked session");
  eq(restored.current, true, "mount#2 latches restored after successful open");
  eq(s2.calls.startNew, 0, "mount#2 NEVER spawns a blank fresh session (replan bug guard)");
  eq(s2.calls.restoreCache, 0, "mount#2 never restores cache");
}

// --- 3) StrictMode double-mount, fresh-start route: only ONE blank session ----
{
  const restored = { current: false };
  // mount #1 fully completes (no params, no cache) → fresh start, latches.
  const s1 = makeSpies();
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: null, restored, cancelled: { current: false },
    createSessionFromAgent: s1.createSessionFromAgent,
    openExistingSession: s1.openExistingSession,
    restoreFromCache: s1.restoreFromCache,
    startNewSession: s1.startNewSession,
    onFromAgentError: s1.onFromAgentError,
  });
  eq(s1.calls.startNew, 1, "mount#1 fresh-start calls startNewSession once");
  eq(restored.current, true, "mount#1 fresh-start latches restored");

  // mount #2 sees the latch already set → does nothing.
  const s2 = makeSpies();
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: null, restored, cancelled: { current: false },
    createSessionFromAgent: s2.createSessionFromAgent,
    openExistingSession: s2.openExistingSession,
    restoreFromCache: s2.restoreFromCache,
    startNewSession: s2.startNewSession,
    onFromAgentError: s2.onFromAgentError,
  });
  eq(s2.calls.startNew, 0, "mount#2 does NOT spawn a second blank session (latch held)");
}

// --- 4) ?session route: opens it, no fresh-start ------------------------------
{
  const s = makeSpies();
  const restored = { current: false };
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: "conv-XYZ", restored, cancelled: { current: false },
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.opened, "conv-XYZ", "?session route opens the requested session");
  eq(restored.current, true, "?session route latches restored");
  eq(s.calls.startNew, 0, "?session route never calls startNewSession");
  eq(s.calls.createFromAgent, 0, "?session route never calls createSessionFromAgent");
}

// --- 5) ?session open FAILS (not cancelled) → falls through to fresh start -----
{
  const s = makeSpies({ openFails: true });
  const restored = { current: false };
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: "conv-DEAD", restored, cancelled: { current: false },
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.calls.openExisting, 1, "?session attempted open once");
  eq(s.opened, "conv-fresh", "failed ?session open falls through to fresh start");
  eq(restored.current, true, "fresh-start after failed open latches restored");
}

// --- 6) from_agent createSessionFromAgent THROWS → fallthrough + error toast ---
{
  const s = makeSpies({ createThrows: true });
  const restored = { current: false };
  await runPlannerBootstrap({
    fromAgentId: 99, sessionParam: null, restored, cancelled: { current: false },
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.calls.fromAgentError, 1, "from_agent error surfaces onFromAgentError once");
  eq(s.opened, "conv-fresh", "from_agent throw falls through to fresh start");
  eq(s.calls.openExisting, 0, "from_agent throw never reaches openExistingSession");
}

// --- 7) local cache hit → restores cache, no fresh-start ----------------------
{
  const s = makeSpies({ cacheHit: true });
  const restored = { current: false };
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: null, restored, cancelled: { current: false },
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.opened, "conv-from-cache", "local cache hit restores the cached session");
  eq(restored.current, true, "cache restore latches restored");
  eq(s.calls.startNew, 0, "cache restore never calls startNewSession");
}

// --- 8) cancelled BEFORE fresh-start backstop → no session opened -------------
{
  const s = makeSpies();
  const restored = { current: false };
  const cancelled = { current: true }; // already cancelled when reaching backstop
  await runPlannerBootstrap({
    fromAgentId: null, sessionParam: null, restored, cancelled,
    createSessionFromAgent: s.createSessionFromAgent,
    openExistingSession: s.openExistingSession,
    restoreFromCache: s.restoreFromCache,
    startNewSession: s.startNewSession,
    onFromAgentError: s.onFromAgentError,
  });
  eq(s.calls.restoreCache, 0, "cancelled mount skips cache restore");
  eq(s.calls.startNew, 0, "cancelled mount skips fresh-start backstop");
  eq(restored.current, false, "cancelled mount leaves latch unset");
}

if (failed) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll planner bootstrap idempotency assertions passed");
