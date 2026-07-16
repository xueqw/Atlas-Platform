#!/usr/bin/env node
/**
 * Deterministic tests for the applied-session → DAG jump entry (change:
 * fix-planner-session-isolation-and-dag-jump, tasks 6.2).
 *
 * Two concerns, both testable without React:
 *   1. The PlannerPanel shows the "open DAG" button iff stage === "applied" AND a
 *      linked_agent_id is present (specs/planner-to-dag-handoff/spec.md).
 *   2. linked_agent_id propagates into the runtime from a session_restored event
 *      (so revisiting an applied session lights up the button).
 *
 * The visibility predicate mirrors PlannerPanel's `showOpenDag`. Keeping it here
 * as the single source of truth documents the contract; if the component diverges
 * this test is the canary.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_dag_jump.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// Mirror of PlannerPanel.showOpenDag.
const showOpenDag = (stage, linkedAgentId) => stage === "applied" && linkedAgentId != null;

// 1) visibility predicate ------------------------------------------------------
check(showOpenDag("applied", 42) === true, "applied + linked id → button shown");
check(showOpenDag("applied", 0) === true, "applied + id 0 → shown (0 is a valid id)");
check(showOpenDag("applied", null) === false, "applied but no linked id → hidden");
check(showOpenDag("ready_to_apply", 42) === false, "linked id but not applied → hidden");
check(showOpenDag("clarifying", null) === false, "fresh session → hidden");

// 2) linked_agent_id propagates via session_restored ---------------------------
const rt = blankRuntime("conv-applied");
check(rt.linkedAgentId === null, "new runtime has no linked agent");
reduceWsEvent(rt, {
  type: "session_restored",
  stage: "applied",
  linked_agent_id: 7,
  messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "done" }],
});
check(rt.linkedAgentId === 7, "session_restored carries linked_agent_id into runtime");
check(rt.stage === "applied", "session_restored sets applied stage");
check(showOpenDag(rt.stage, rt.linkedAgentId) === true, "restored applied session lights up the DAG button");

// session_restored without linked_agent_id leaves it null.
const rt2 = blankRuntime("conv-draft");
reduceWsEvent(rt2, { type: "session_restored", stage: "clarifying" });
check(rt2.linkedAgentId === null, "restore without linked_agent_id keeps it null");
check(showOpenDag(rt2.stage, rt2.linkedAgentId) === false, "non-applied restored session hides the button");

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nALL PASS");
