#!/usr/bin/env node
/**
 * Deterministic tests for change:
 * planner-model-switch-concurrency-and-final-render.
 *
 * Exercises the pure reducer + helpers in lib/planner-session.ts so the
 * product rules are provable without a browser/backend:
 *
 *   #2  model_resolved routes a model switch into the owning runtime only.
 *   #3  "working" derivation (thinking || streamBuf) drives the concurrency
 *       count; a session never blocks itself.
 *   #4  the final proposal is delivered via final_summary (carrying the
 *       structured proposal) + a proposal.json file chip on the assistant
 *       message — the raw JSON is never rendered as chat text.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_model_switch_and_final_render.test.mjs
 */
import {
  blankRuntime,
  reduceWsEvent,
  extractProposalFile,
} from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}
function eq(actual, expected, name) {
  check(actual === expected, `${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// Mirror of the page's working-set derivation (single source of truth = runtime).
function isWorking(rt) {
  return rt.thinking || rt.streamBuf.length > 0;
}
function workingIds(...runtimes) {
  return runtimes.filter(isWorking).map((r) => r.conversationId);
}
// Mirror of the page's concurrency gate.
const MAX_WORKING = 3;
function blocked(workingList, cid) {
  return workingList.length >= MAX_WORKING && !workingList.includes(cid);
}

// ── #2 model switch is per-session ───────────────────────────────────────────
{
  const A = blankRuntime("conv-A");
  const B = blankRuntime("conv-B");
  reduceWsEvent(A, { type: "model_resolved", model: "gpt-5.4", provider: "openai" });
  eq(A.model, "gpt-5.4", "A picks up switched model");
  eq(A.provider, "openai", "A picks up switched provider");
eq(B.model, "glm-4-flash", "B model untouched by A's switch (isolation)");
}

// ── #3 working derivation + concurrency gate ─────────────────────────────────
{
  const A = blankRuntime("conv-A");
  const B = blankRuntime("conv-B");
  const C = blankRuntime("conv-C");
  const D = blankRuntime("conv-D");

  eq(workingIds(A, B, C, D).length, 0, "no sessions working initially");

  // A thinking, B streaming → both working.
  reduceWsEvent(A, { type: "thinking_content", content: "…" });
  reduceWsEvent(B, { type: "token", content: "hi" });
  let w = workingIds(A, B, C, D);
  eq(w.length, 2, "thinking + streaming both count as working");
  check(w.includes("conv-A") && w.includes("conv-B"), "working set has A and B");

  // C also starts → 3 working. Idle D is blocked from starting a 4th.
  reduceWsEvent(C, { type: "thinking_content", content: "…" });
  w = workingIds(A, B, C, D);
  eq(w.length, 3, "three working");
  check(blocked(w, "conv-D"), "4th idle session is blocked at 3 working");
  // A (already working) is never blocked from continuing itself.
  check(!blocked(w, "conv-A"), "a working session never blocks itself");

  // A finishes (done clears thinking + streamBuf) → frees a slot.
  reduceWsEvent(A, { type: "done" });
  w = workingIds(A, B, C, D);
  eq(w.length, 2, "done frees a working slot");
  check(!isWorking(A), "A no longer working after done");
  check(!blocked(w, "conv-D"), "D allowed once a slot frees up");
}

// ── #4 final proposal delivered as file chip, not chat JSON ───────────────────
{
  const A = blankRuntime("conv-A");
  // Live-streamed prose only (the backend withholds the JSON fence).
  reduceWsEvent(A, { type: "token", content: "方案如下：" });
  // final_summary carries the structured proposal + the proposal.json artifact.
  const proposal = { architecture_summary: "P+Agent+M", nodes: [{ id: "a", type: "agent", config: {} }], edges: [], rationale: "r" };
  const fsEff = reduceWsEvent(A, {
    type: "final_summary",
    text: "✅ 方案已就绪，完整内容见 proposal.json。",
    files: [{ path: "conv-A/proposal.json", filename: "proposal.json", kind: "proposal" }],
    proposal,
  });
  check(fsEff.changed === true, "final_summary repaints");
  eq(A.stage, "ready_to_apply", "final_summary moves stage to ready_to_apply");
  check(A.proposal !== null, "proposal captured from final_summary (drives PlannerPanel + apply)");
  eq(A.pendingProposalFile, "proposal.json", "pending proposal file recorded for the upcoming done");

  // done stamps the assistant message with the file chip and NO JSON text.
  reduceWsEvent(A, { type: "done" });
  const last = A.messages[A.messages.length - 1];
  eq(last.role, "assistant", "final turn is an assistant message");
  eq(last.proposalFile, "proposal.json", "assistant message carries proposalFile chip");
  check(!last.content.includes("{"), "chat bubble contains no JSON braces");
  check(!last.content.includes("ready"), "chat bubble contains no proposal JSON keys");
  check(last.content.includes("方案如下"), "live prose preserved in the bubble");
  eq(A.pendingProposalFile, null, "pending flag cleared after done");
}

// ── #4 restore path: marker parsed into a chip ───────────────────────────────
{
  eq(
    extractProposalFile("方案如下：\n\n<proposal_file>proposal.json</proposal_file>").content,
    "方案如下：",
    "extractProposalFile strips the marker from display text",
  );
  eq(
    extractProposalFile("方案如下：\n\n<proposal_file>proposal.json</proposal_file>").proposalFile,
    "proposal.json",
    "extractProposalFile returns the referenced filename",
  );
  eq(extractProposalFile("普通文本").proposalFile, null, "no marker → no proposalFile");

  const A = blankRuntime("conv-A");
  reduceWsEvent(A, {
    type: "session_restored",
    messages: [
      { role: "user", content: "做个客服" },
      { role: "assistant", content: "方案如下：\n\n<proposal_file>proposal.json</proposal_file>" },
    ],
  });
  const restored = A.messages[A.messages.length - 1];
  eq(restored.proposalFile, "proposal.json", "restored proposal turn renders a chip");
  check(!restored.content.includes("<proposal_file>"), "restored bubble has no raw marker");
}

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nALL PASS");
