#!/usr/bin/env node
/**
 * Deterministic tests for the planner skill-transparency reducer (change:
 * add-planner-skill-transparency, task 4.5).
 *
 * Contract (spec planner-skill-transparency):
 *   - an activity carrying skill / skill_action passes them through to the step
 *     (start and done branches) AND accumulates the skill as triggered this turn.
 *   - on done, the appended assistant message carries triggeredSkills (from the
 *     done payload, or falling back to what the activities accumulated).
 *   - a turn that triggered nothing leaves the message with no triggeredSkills.
 *   - session_restored re-hydrates triggeredSkills from persisted messages.
 *   - legacy events (no new fields) are unaffected.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_skill_transparency.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// 1) activity passes through skill / skillAction + accumulates triggered -------
{
  const rt = blankRuntime("conv-act");
  reduceWsEvent(rt, {
    type: "activity", id: "t1-confirm", phase: "awaiting_confirmation",
    label: "等待确认", status: "start", skill: "a2ui", skill_action: "生成结构化确认卡片",
  });
  check(rt.activities[0].skill === "a2ui", "start step carries skill");
  check(rt.activities[0].skillAction === "生成结构化确认卡片", "start step carries skillAction");
  check((rt.pendingTriggeredSkills || []).join(",") === "a2ui", "skill accumulated as triggered");
  reduceWsEvent(rt, {
    type: "activity", id: "t1-confirm", phase: "awaiting_confirmation",
    label: "等待确认", status: "done", skill: "a2ui", skill_action: "生成结构化确认卡片",
  });
  check(rt.activities.length === 1 && rt.activities[0].status === "done", "done pairs by id");
  check(rt.activities[0].skill === "a2ui", "done step retains skill");
}

// 2) done stamps triggeredSkills onto the assistant message --------------------
{
  const rt = blankRuntime("conv-done");
  reduceWsEvent(rt, {
    type: "activity", id: "t1-confirm", phase: "awaiting_confirmation",
    label: "等待确认", status: "start", skill: "a2ui", skill_action: "生成结构化确认卡片",
  });
  reduceWsEvent(rt, { type: "token", content: "回复内容" });
  reduceWsEvent(rt, { type: "done", triggered_skills: ["a2ui"] });
  const asst = rt.messages.filter((m) => m.role === "assistant");
  check(asst.length === 1, "one assistant message appended");
  check((asst[0].triggeredSkills || []).join(",") === "a2ui", "done stamps triggeredSkills");
  check(rt.pendingTriggeredSkills === null, "pendingTriggeredSkills cleared after done");
}

// 3) done with no payload falls back to accumulated triggers -------------------
{
  const rt = blankRuntime("conv-fallback");
  reduceWsEvent(rt, {
    type: "activity", id: "t1-proposal", phase: "composing_proposal",
    label: "生成方案", status: "done", skill: "deerflow-planner", skill_action: "整理需求摘要与任务分型",
  });
  reduceWsEvent(rt, { type: "token", content: "回复" });
  reduceWsEvent(rt, { type: "done" });
  const asst = rt.messages.filter((m) => m.role === "assistant");
  check((asst[0].triggeredSkills || []).join(",") === "deerflow-planner", "falls back to accumulated triggers");
}

// 4) a turn that triggered nothing leaves no badge -----------------------------
{
  const rt = blankRuntime("conv-none");
  reduceWsEvent(rt, { type: "activity", id: "t1-processing", phase: "processing", label: "理解需求", status: "start" });
  check(rt.activities[0].skill === undefined, "non-skill activity carries no skill");
  reduceWsEvent(rt, { type: "token", content: "纯回复" });
  reduceWsEvent(rt, { type: "done" });
  const asst = rt.messages.filter((m) => m.role === "assistant");
  check(asst[0].triggeredSkills === undefined, "no badge when nothing was triggered");
}

// 5) session_restored re-hydrates triggeredSkills from persisted messages ------
{
  const rt = blankRuntime("conv-restore");
  reduceWsEvent(rt, {
    type: "session_restored",
    messages: [
      { role: "user", content: "需求" },
      { role: "assistant", content: "方案回复", triggered_skills: ["a2ui", "deerflow-planner"] },
    ],
  });
  const asst = rt.messages.filter((m) => m.role === "assistant");
  check(asst.length === 1, "restored one assistant message");
  check((asst[0].triggeredSkills || []).join(",") === "a2ui,deerflow-planner", "triggeredSkills restored from persistence");
}

// 6) final_summary primes triggered skills for the upcoming message ------------
{
  const rt = blankRuntime("conv-final");
  reduceWsEvent(rt, { type: "final_summary", text: "摘要", triggered_skills: ["deerflow-planner"] });
  check((rt.pendingTriggeredSkills || []).join(",") === "deerflow-planner", "final_summary primes triggered skills");
  reduceWsEvent(rt, { type: "done" });
  const asst = rt.messages.filter((m) => m.role === "assistant");
  check((asst[0].triggeredSkills || []).join(",") === "deerflow-planner", "primed triggers stamped on done");
}

// 7) session isolation: a skill activity for A never touches B ------------------
{
  const a = blankRuntime("conv-A");
  const b = blankRuntime("conv-B");
  reduceWsEvent(a, {
    type: "activity", id: "t1-confirm", phase: "awaiting_confirmation",
    label: "等待确认", status: "start", skill: "a2ui", skill_action: "生成结构化确认卡片",
  });
  check((a.pendingTriggeredSkills || []).length === 1, "A accumulated its trigger");
  check(b.pendingTriggeredSkills === null, "B untouched by A's skill activity");
}

if (failed > 0) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nAll skill-transparency reducer tests passed");
