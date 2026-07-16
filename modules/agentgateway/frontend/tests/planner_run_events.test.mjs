#!/usr/bin/env node
/**
 * Deterministic tests for the planner run-event reducer (change:
 * builder-frontend-minimal-loop, T7 consuming T8's run_event stream).
 *
 * Contract (spec builder-frontend-loop / planner-run-events):
 *   - a run_event appends a step; a later event for the same step updates in place
 *     (running → completed), never duplicating.
 *   - run_event coexists with the activity stream (both accumulate independently).
 *   - unknown / malformed events are ignored gracefully (no throw, no change).
 *   - runEvents reset at session_restored (reconnect cleanup).
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_run_events.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// 1) run_event appends, same step updates in place -----------------------------
{
  const rt = blankRuntime("conv-r");
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "context_load", status: "running", message: "聚合中" });
  check(rt.runEvents.length === 1, "first run_event appends");
  check(rt.runEvents[0].status === "running", "status running recorded");
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "context_load", status: "completed", message: "完成", progress: 0.25 });
  check(rt.runEvents.length === 1, "same step updates in place (no dup)");
  check(rt.runEvents[0].status === "completed", "status flipped to completed");
  check(rt.runEvents[0].progress === 0.25, "progress carried");
}

// 2) distinct steps accumulate -------------------------------------------------
{
  const rt = blankRuntime("conv-r2");
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "expert_retrieval", status: "completed", message: "命中 2 个" });
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "capability_match", status: "completed", message: "匹配 5 个候选能力" });
  check(rt.runEvents.length === 2, "distinct steps accumulate");
  const cap = rt.runEvents.find((e) => e.step === "capability_match");
  check(cap && cap.message.includes("候选能力"), "stuck-step message is specific");
}

// 3) proposal_id carried on proposal_compose -----------------------------------
{
  const rt = blankRuntime("conv-r3");
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "proposal_compose", status: "completed", proposal_id: 42 });
  const ev = rt.runEvents.find((e) => e.step === "proposal_compose");
  check(ev && ev.proposal_id === 42, "proposal_compose event carries proposal_id");
}

// 4) coexists with activity ----------------------------------------------------
{
  const rt = blankRuntime("conv-r4");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "context_load", status: "running" });
  check(rt.activities.length === 1, "activity stream still works");
  check(rt.runEvents.length === 1, "run_event stream added alongside");
}

// 5) malformed / unknown ignored gracefully ------------------------------------
{
  const rt = blankRuntime("conv-r5");
  const before = rt.runEvents.length;
  reduceWsEvent(rt, { type: "run_event", run_id: "r1" }); // no step/status
  reduceWsEvent(rt, { type: "totally_unknown_type", foo: 1 });
  check(rt.runEvents.length === before, "malformed/unknown run events do not append or throw");
}

// 6) session_restored resets runEvents -----------------------------------------
{
  const rt = blankRuntime("conv-r6");
  reduceWsEvent(rt, { type: "run_event", run_id: "r1", step: "context_load", status: "completed" });
  check(rt.runEvents.length === 1, "precondition: one event");
  reduceWsEvent(rt, { type: "session_restored", messages: [], memory: null });
  check(rt.runEvents.length === 0, "session_restored clears runEvents (reconnect cleanup)");
}

if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nall run-event reducer tests passed");
