#!/usr/bin/env node
/**
 * Deterministic tests for the planner activity timeline reducer (change:
 * planner-activity-visibility-and-thinking-fix, tasks 3.x + 5.1).
 *
 * Contract (spec planner-activity-timeline):
 *   - activity `start` appends an in-progress step; `done` with the same id flips
 *     it to completed.
 *   - on turn end (done / error) any still-active step is finalized — the
 *     timeline never hangs "in progress".
 *   - activities live on the owning runtime (session isolation): an event for A
 *     never touches B.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_activity_timeline.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// 1) start → in progress, done → completed -------------------------------------
{
  const rt = blankRuntime("conv-a");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  check(rt.activities.length === 1, "start appends a step");
  check(rt.activities[0].status === "active", "started step is active (in progress)");
  check(rt.activities[0].label === "理解需求", "step carries its label");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "done" });
  check(rt.activities.length === 1, "done pairs by id (no duplicate step)");
  check(rt.activities[0].status === "done", "step flips to done");
}

// 2) multi-step ordered timeline -----------------------------------------------
{
  const rt = blankRuntime("conv-multi");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "done" });
  reduceWsEvent(rt, { type: "activity", id: "t1-drafting", phase: "drafting", label: "起草回复", status: "start" });
  check(rt.activities.length === 2, "two steps recorded");
  check(rt.activities[0].phase === "processing" && rt.activities[1].phase === "drafting", "steps kept in arrival order");
  check(rt.activities[1].status === "active", "latest step still in progress");
}

// 3) done finalizes any unfinished step ----------------------------------------
{
  const rt = blankRuntime("conv-finalize");
  reduceWsEvent(rt, { type: "activity", id: "t1-drafting", phase: "drafting", label: "起草回复", status: "start" });
  check(rt.activities.some((a) => a.status === "active"), "an active step before done");
  reduceWsEvent(rt, { type: "done" });
  check(rt.activities.every((a) => a.status === "done"), "done finalizes all active steps");
}

// 4) error also finalizes ------------------------------------------------------
{
  const rt = blankRuntime("conv-err");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  reduceWsEvent(rt, { type: "error", message: "boom" });
  check(rt.activities.every((a) => a.status === "done"), "error finalizes active steps (no spinner left)");
}

// 5) done frame without a prior start synthesizes a completed step -------------
{
  const rt = blankRuntime("conv-lostframe");
  reduceWsEvent(rt, { type: "activity", id: "t1-proposal", phase: "composing_proposal", label: "生成方案", status: "done" });
  check(rt.activities.length === 1 && rt.activities[0].status === "done", "lone done synthesizes a completed step");
}

// 6) session isolation: A's activity never touches B ---------------------------
{
  const a = blankRuntime("conv-A");
  const b = blankRuntime("conv-B");
  reduceWsEvent(a, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  check(a.activities.length === 1, "A has its step");
  check(b.activities.length === 0, "B is untouched by A's activity");
}

// 7) unknown activity status is a no-op ----------------------------------------
{
  const rt = blankRuntime("conv-unknown");
  const eff = reduceWsEvent(rt, { type: "activity", id: "x", phase: "p", label: "l", status: "weird" });
  check(eff.changed === false, "unknown activity status → changed:false");
  check(rt.activities.length === 0, "unknown status appends nothing");
}

if (failed > 0) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nAll activity-timeline tests passed");
